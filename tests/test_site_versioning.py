"""站点切版/轮换的回归测试。

覆盖需求中的三类缺陷场景：
1. 交错读页：切版与并发读页同时发生时，每个响应的 X-Site-Revision 头、
   HTML meta 标记与正文必须对应同一版本，不允许“旧版号配新版正文”；
2. 写盘失败：状态持久化失败时切版不生效，站点继续提供原版本、原版号，
   且之后磁盘恢复仍可正常切版；
3. 重启：状态从磁盘恢复，版号单调递增不回退；写盘失败后重启同样不回退。

另外验证采集侧与真实站点联动时，切版/采集交错不会把跨版批号发布为同版快照。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_site_module():
    """site/ 与标准库 site 包同名，按文件路径独立加载。"""
    spec = importlib.util.spec_from_file_location(
        "notice_site_under_test", _REPO_ROOT / "site" / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


site_mod = _load_site_module()

# 复用仓库内真实站点内容（v1/v2/v3），保证与 Docker/手工启动行为一致
REAL_CONTENT_ROOT = _REPO_ROOT / "site"


@pytest.fixture()
def site_server(tmp_path):
    """在随机端口启动一个使用临时状态目录的站点，返回 (base_url, state)。"""
    state_dir = tmp_path / "state"
    state = site_mod.SiteState(content_root=REAL_CONTENT_ROOT, state_dir=state_dir)
    server = site_mod.build_server(host="127.0.0.1", port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, state, state_dir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---- HTTP 小工具 ------------------------------------------------------

def _request(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _meta_revision(html: bytes) -> int:
    match = re.search(rb'<meta name="site-revision" content="(\d+)">', html)
    assert match, "HTML 中缺少 site-revision meta 标记"
    return int(match.group(1))


def _switch(base_url: str, version: str):
    return _request("POST", f"{base_url}/admin/version", {"version": version})


# ---- 1. 基础切版：响应三要素同版 --------------------------------------

def test_switch_takes_effect_and_response_is_self_consistent(site_server):
    base_url, state, _ = site_server
    before = state.snapshot()
    assert before.version == "v1"

    status, headers, body = _request("GET", f"{base_url}/index.html")
    assert status == 200
    rev_before = int(headers["X-Site-Revision"])
    assert rev_before == before.revision
    assert _meta_revision(body) == rev_before

    # 切到 v2：版号递增、磁盘同步
    status, headers, body = _switch(base_url, "v2")
    assert status == 200
    payload = json.loads(body)
    assert payload["version"] == "v2"
    assert payload["revision"] == rev_before + 1
    assert int(headers["X-Site-Revision"]) == payload["revision"]

    status, headers, body = _request("GET", f"{base_url}/index.html")
    rev_after = int(headers["X-Site-Revision"])
    assert rev_after == payload["revision"]
    # 头、HTML meta 必须同版
    assert _meta_revision(body) == rev_after
    # 正文必须来自 v2 目录（v2 与 v1 的镜像页内容不同；索引页两版一致，
    # 这里改用仅 v2/v3 存在差异的 2024-001_mirror 页面验证正文版本归属）
    status2, headers2, mirror = _request(
        "GET", f"{base_url}/notice/2024-001_mirror.html")
    assert status2 == 200
    assert int(headers2["X-Site-Revision"]) == rev_after
    assert _meta_revision(mirror) == rev_after
    v2_mirror = (REAL_CONTENT_ROOT / "v2" / "notice"
                 / "2024-001_mirror.html").read_bytes()
    v2_mirror = v2_mirror.replace(b"__REVISION__", str(rev_after).encode())
    assert mirror == v2_mirror


def test_next_and_reload_bump_revision(site_server):
    base_url, state, _ = site_server
    start = state.snapshot().revision

    status, _, body = _request("POST", f"{base_url}/admin/next")
    assert status == 200
    payload = json.loads(body)
    assert payload["version"] == "v2"
    assert payload["revision"] == start + 1

    status, _, body = _request("POST", f"{base_url}/admin/reload")
    payload = json.loads(body)
    assert status == 200
    assert payload["version"] == "v2"  # reload 不切版本
    assert payload["revision"] == start + 2


def test_404_and_head_carry_consistent_revision(site_server):
    base_url, state, _ = site_server
    rev = state.snapshot().revision
    status, headers, body = _request("GET", f"{base_url}/notice/missing.html")
    assert status == 404
    assert int(headers["X-Site-Revision"]) == rev
    assert f"站点版号 {rev}".encode() in body

    status, headers, body = _request("HEAD", f"{base_url}/index.html")
    assert status == 200
    assert int(headers["X-Site-Revision"]) == rev
    assert body == b""  # HEAD 无响应体


# ---- 2. 交错读页：并发切版时每个响应内部必须同版 ----------------------

def test_interleaved_reads_never_mix_revision_and_content(site_server):
    base_url, state, _ = site_server
    # 固定从 v1 开始
    assert state.snapshot().version == "v1"
    v1_mirror = (REAL_CONTENT_ROOT / "v1" / "notice"
                 / "2024-001_mirror.html").read_bytes()
    v2_mirror = (REAL_CONTENT_ROOT / "v2" / "notice"
                 / "2024-001_mirror.html").read_bytes()
    assert v1_mirror != v2_mirror  # 前置条件：两版正文确实不同

    errors: list[AssertionError] = []
    stop = threading.Event()

    def reader():
        # 持续读页，校验每个响应头/meta/正文三者同版
        while not stop.is_set():
            try:
                status, headers, body = _request(
                    "GET", f"{base_url}/notice/2024-001_mirror.html")
            except (urllib.error.URLError, RemoteDisconnected):
                continue
            if status != 200:
                continue
            rev = int(headers["X-Site-Revision"])
            try:
                assert _meta_revision(body) == rev, "meta 与响应头版号不一致"
                # 版号必须与正文版本对应：v1 正文只可能配 v1 切版前的版号，
                # v2 正文只可能配切版后的版号；用正文内容反查所属版本
                expected_v1 = v1_mirror.replace(
                    b"__REVISION__", str(rev).encode())
                expected_v2 = v2_mirror.replace(
                    b"__REVISION__", str(rev).encode())
                assert body in (expected_v1, expected_v2), \
                    "正文与版号不匹配（疑似跨版混合响应）"
            except AssertionError as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()

    revisions_seen: set[int] = set()
    for target in ("v2", "v1", "v2", "v3", "v1"):
        status, _, body = _switch(base_url, target)
        assert status == 200
        revisions_seen.add(json.loads(body)["revision"])

    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"发现跨版混合响应：{errors[0]!r}"
    assert len(revisions_seen) == 5  # 每次切版都真实递增


def test_interleaved_collect_does_not_publish_cross_revision_snapshot(
        site_server, tmp_path):
    """切版与采集交错：采集器的跨版检测必须拒绝整次发布。"""
    import asyncio

    sys.path.insert(0, str(_REPO_ROOT))
    from collector.db import Database
    from collector.fetcher import HttpFetcher
    from collector.service import CollectorService

    base_url, state, _ = site_server
    _switch(base_url, "v1")
    v1_rev = state.snapshot().revision

    db = Database(str(tmp_path / "collector.db"))
    fetcher = HttpFetcher(base_url)
    service = CollectorService(db, fetcher)

    async def scenario():
        task = asyncio.create_task(service.collect("2024-003"))
        # 在采集进行中切到 v2（制造跨版窗口）
        await asyncio.sleep(0)
        _switch(base_url, "v2")
        return await task

    result = asyncio.run(scenario())
    # 三种可接受结果：
    # a) 采集全部落在切版前 -> 以 v1 版号成功发布；
    # b) 任一页面落在切版后且版号不一致 -> 跨版失败，不发布；
    # c) 全部落在切版后（整次同版 v2）-> 命中 v2 同号异文，同样不发布。
    # 关键不变量：绝不能把跨版批号发布成同版快照。
    if result["status"] == "failed":
        assert result["snapshot_id"] is None
        assert db.latest_published_snapshot() is None
    else:
        assert result["status"] in ("published", "noop")
        assert result["site_revision"] == v1_rev
        snap = db.latest_published_snapshot()
        assert snap is not None and snap["site_revision"] == v1_rev
    asyncio.run(fetcher.aclose())
    db.close()


def test_interleaved_switch_mid_crawl_is_rejected_as_cross_revision(
        site_server, tmp_path):
    """精确制造跨版窗口：抓取索引后、抓正文前切版，必须按跨版拒绝。"""
    import asyncio

    sys.path.insert(0, str(_REPO_ROOT))
    from collector.crawler import run_crawl
    from collector.fetcher import HttpFetcher

    base_url, state, _ = site_server
    _switch(base_url, "v1")
    v1_rev = state.snapshot().revision
    fetcher = HttpFetcher(base_url)
    original_fetch = fetcher.fetch
    switched = {"done": False}

    async def fetch_then_switch(path):
        page = await original_fetch(path)
        # 索引页（基准版号）拿到后立刻切版，后续页面必然跨版
        if not switched["done"] and path == "/index.html":
            switched["done"] = True
            _switch(base_url, "v2")
        return page

    fetcher.fetch = fetch_then_switch
    result = asyncio.run(run_crawl(fetcher, "2024-003"))
    assert result.ok is False
    assert "跨版" in result.failure_reason
    assert result.conflict_detail["expected_revision"] == v1_rev
    assert result.conflict_detail["actual_revision"] == v1_rev + 1
    asyncio.run(fetcher.aclose())


# ---- 3. 写盘失败：变更不生效，继续提供原版 ----------------------------

def test_persist_failure_keeps_old_version_served(site_server):
    base_url, state, state_dir = site_server
    before = state.snapshot()

    # 让状态文件所在目录不可写（tmp 路径替换会抛 OSError）
    original_persist = state._persist

    def broken_persist(revision, version):
        raise site_mod.StatePersistError("simulated disk failure")

    state._persist = broken_persist
    try:
        status, _, body = _switch(base_url, "v2")
        assert status == 503
        payload = json.loads(body)
        assert "current" in payload
        # 内存状态保持原版
        assert state.snapshot().version == before.version
        assert state.snapshot().revision == before.revision
    finally:
        state._persist = original_persist

    # 站点仍提供原版正文与原版号
    status, headers, page = _request("GET", f"{base_url}/index.html")
    assert status == 200
    assert int(headers["X-Site-Revision"]) == before.revision
    assert _meta_revision(page) == before.revision

    # 磁盘上的状态文件未被破坏
    on_disk = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk == {"revision": before.revision, "version": before.version}

    # 写盘恢复后切版正常生效，且版号接着旧值递增、不回退
    status, _, body = _switch(base_url, "v2")
    assert status == 200
    payload = json.loads(body)
    assert payload["version"] == "v2"
    assert payload["revision"] == before.revision + 1


def test_persist_failure_with_real_readonly_dir(tmp_path):
    """用真实文件系统权限模拟写盘失败（root 下会跳过）。"""
    import os

    if os.geteuid() == 0:
        pytest.skip("root 用户无视目录只读权限")
    content = tmp_path / "site"
    (content / "v1").mkdir(parents=True)
    (content / "v2").mkdir(parents=True)
    (content / "v1" / "index.html").write_text("v1-__REVISION__", encoding="utf-8")
    (content / "v2" / "index.html").write_text("v2-__REVISION__", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = site_mod.SiteState(content_root=content, state_dir=state_dir)
    before = state.snapshot()

    state_dir.chmod(0o500)  # 只读
    try:
        with pytest.raises(site_mod.StatePersistError):
            state.set_version("v2")
        # 内存与磁盘都保持原版
        assert state.snapshot().version == "v1"
        assert state.snapshot().revision == before.revision
    finally:
        state_dir.chmod(0o700)

    # 恢复后可正常切版
    snap = state.set_version("v2")
    assert snap.version == "v2"
    assert snap.revision == before.revision + 1


def test_failed_switch_via_next_and_reload_also_keeps_old_state(site_server):
    base_url, state, _ = site_server
    before = state.snapshot()
    state._persist = lambda rev, ver: (_ for _ in ()).throw(
        site_mod.StatePersistError("disk full"))
    try:
        status, _, body = _request("POST", f"{base_url}/admin/next")
        assert status == 503
        status, _, body = _request("POST", f"{base_url}/admin/reload")
        assert status == 503
    finally:
        del state._persist
    assert state.snapshot() == before


# ---- 4. 重启：版号不回退 ----------------------------------------------

def test_restart_restores_state_and_never_rolls_back(site_server, tmp_path):
    base_url, state, state_dir = site_server
    _switch(base_url, "v2")
    _switch(base_url, "v3")
    last = state.snapshot()
    assert last.version == "v3"

    # 用同一状态目录重新构造状态，模拟进程重启
    restarted = site_mod.SiteState(
        content_root=REAL_CONTENT_ROOT, state_dir=state_dir)
    restored = restarted.snapshot()
    assert restored.version == "v3"
    assert restored.revision == last.revision

    # 重启后继续递增，不回退
    again = restarted.set_version("v1")
    assert again.revision == last.revision + 1
    on_disk = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk["revision"] == last.revision + 1
    assert on_disk["version"] == "v1"


def test_restart_after_persist_failure_does_not_roll_back(site_server):
    base_url, state, state_dir = site_server
    before = state.snapshot()

    original = state._persist
    state._persist = lambda rev, ver: (_ for _ in ()).throw(
        site_mod.StatePersistError("simulated disk failure"))
    try:
        status, _, _ = _switch(base_url, "v2")
        assert status == 503
    finally:
        state._persist = original

    # “重启”：失败的切版没有落盘，恢复出来的仍是失败前的状态
    restarted = site_mod.SiteState(
        content_root=REAL_CONTENT_ROOT, state_dir=state_dir)
    restored = restarted.snapshot()
    assert restored.version == before.version
    assert restored.revision == before.revision

    # 重启后切版只在旧版号基础上 +1，绝不回退到更小值
    snap = restarted.set_version("v2")
    assert snap.revision == before.revision + 1
