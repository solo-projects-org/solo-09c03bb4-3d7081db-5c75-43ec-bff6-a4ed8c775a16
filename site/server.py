"""本地药品召回公告站（仅依赖 Python 标准库）。

特点：
- 内容按版本目录（v1/v2/v3 ...）组织，目录可热插拔；
- 每次切换版本 / reload 都让全站版号（revision）单调递增；
- 每个响应（含 404）都通过 X-Site-Revision 回传当前版号；
- HTML 中的 __REVISION__ 占位符会被替换为当前版号；
- 版号状态持久化到磁盘，重启后继续递增，不回退。

正确性保证（修复版）：
- 切版/轮换/reload 采用“先写盘、后生效”：状态 JSON 原子落盘成功后才提交
  内存中的新 (version, revision)；落盘失败时原样保留旧版，对外服务不受影响，
  重启后版号绝不回退。
- 每个请求在同一把锁内取一次不可变快照 (version, revision)，随后的路径解析、
  正文读取、占位符替换与 X-Site-Revision 响应头全部基于该快照，
- 因此同一响应的响应版号、HTML meta 标记与正文必然对应同一个站点版本；
  切版与读页并发交错时，进行中的请求要么完整看到旧版、要么完整看到新版，
  不会出现“旧版号配新版正文”的混合响应。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CONTENT_ROOT = Path(os.environ.get("SITE_CONTENT_ROOT", "/srv/site"))
STATE_DIR = Path(os.environ.get("SITE_STATE_DIR", "/data/site"))
HOST = os.environ.get("SITE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SITE_PORT", "8080"))

_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css",
}


class StatePersistError(Exception):
    """状态持久化失败：新状态尚未生效，站点继续提供原版本。"""


@dataclass(frozen=True)
class StateSnapshot:
    """某一时刻不可变的站点状态，单个请求自始至终只使用这一个快照。"""

    revision: int
    version: str
    available_versions: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"revision": self.revision, "version": self.version,
                "available_versions": list(self.available_versions)}


class SiteState:
    """线程安全的站点版号状态，持久化到 JSON 文件。

    所有状态变更（set_version/next_version/reload）都遵循
    “准备新状态 -> 原子写盘 -> 提交内存”的顺序；写盘异常不会污染内存状态。
    """

    def __init__(self, content_root: Path | None = None,
                 state_dir: Path | None = None) -> None:
        self._content_root = content_root if content_root is not None else CONTENT_ROOT
        self._state_dir = state_dir if state_dir is not None else STATE_DIR
        self._state_file = self._state_dir / "state.json"
        # RLock：变更方法持锁时会再调用 snapshot()
        self._lock = threading.RLock()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if self._state_file.exists():
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._revision = int(data["revision"])
            self._version = str(data["version"])
        else:
            # 首次启动：初始状态先持久化为 revision=1（与历史版号约定一致），
            # 写盘成功后才对外提供服务
            self._revision = 1
            self._version = "v1"
            self._persist(self._revision, self._version)

    @property
    def content_root(self) -> Path:
        return self._content_root

    # ---- 内部工具（调用方需持有 _lock 或在构造期单线程调用） --------
    def _available_versions(self) -> list[str]:
        if not self._content_root.exists():
            return []
        return sorted(p.name for p in self._content_root.iterdir()
                      if p.is_dir() and p.name.startswith("v"))

    def _persist(self, revision: int, version: str) -> None:
        """原子写入状态文件；失败抛 StatePersistError，不改动任何内存状态。"""
        tmp = self._state_file.with_name(self._state_file.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps({"revision": revision, "version": version},
                           ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except OSError as exc:
            # 清理可能残留的临时文件，忽略清理本身的失败
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StatePersistError(
                f"状态文件写入失败（{self._state_file}）：{exc!r}；"
                "本次变更不生效，站点继续提供原版本"
            ) from exc

    def _commit(self, new_version: str) -> StateSnapshot:
        """计算新 revision、先持久化再提交内存。锁内调用。"""
        new_revision = self._revision + 1
        # 防御：版号必须单调递增，任何路径都不允许回退
        if new_revision <= self._revision:
            raise StatePersistError(
                f"拒绝版号回退：新版号 {new_revision} 不大于当前版号 {self._revision}")
        # 先写盘：只有持久化成功，内存状态才允许变化
        self._persist(new_revision, new_version)
        self._revision = new_revision
        self._version = new_version
        return self.snapshot()

    # ---- 只读视图 -------------------------------------------------
    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return StateSnapshot(
                revision=self._revision,
                version=self._version,
                available_versions=tuple(self._available_versions()),
            )

    # ---- 状态变更：先持久化、后生效 --------------------------------
    def set_version(self, version: str) -> StateSnapshot:
        with self._lock:
            target = self._content_root / version
            if not target.is_dir():
                raise KeyError(version)
            return self._commit(version)

    def next_version(self) -> StateSnapshot:
        with self._lock:
            versions = self._available_versions()
            if not versions:
                raise RuntimeError("no version directories found")
            try:
                idx = versions.index(self._version)
                nxt = versions[(idx + 1) % len(versions)]
            except ValueError:
                nxt = versions[0]
            return self._commit(nxt)

    def reload(self) -> StateSnapshot:
        """内容目录被外部改动后调用：不切版本，仅递增全站版号。"""
        with self._lock:
            return self._commit(self._version)


# 默认全局状态懒加载：避免在导入阶段（如测试收集、无 /data 写权限的环境）
# 就强制创建状态目录；只有真正启动服务器或显式调用 get_state() 时才初始化。
_STATE: SiteState | None = None
_STATE_LOCK = threading.Lock()


def get_state() -> SiteState:
    global _STATE
    if _STATE is None:
        with _STATE_LOCK:
            if _STATE is None:
                _STATE = SiteState()
    return _STATE


class _Handler(BaseHTTPRequestHandler):
    server_version = "RecallNoticeSite/1.0"

    # state 由 build_server 绑定到具体 Handler 子类；默认懒加载全局状态
    state: SiteState | None = None

    # ---- 基础工具 -------------------------------------------------
    def _send_json(self, status: int, payload: dict,
                   snap: StateSnapshot | None = None) -> None:
        snap = snap or self.state.snapshot()
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Site-Revision", str(snap.revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        print("[site] " + fmt % args)

    # ---- 管理 API -------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        # HEAD 与 GET 走同一套版号/路径逻辑，仅不回响应体
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/admin/state":
            self._send_json(HTTPStatus.OK, self.state.snapshot().as_dict())
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json body"})
            return

        if path == "/admin/version":
            version = str(payload.get("version", "")).strip()
            if not version:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing 'version'"})
                return
            try:
                snap = self.state.set_version(version)
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"error": f"version directory not found: {version}"})
                return
            except StatePersistError as exc:
                # 写盘失败：变更未生效，响应明确告知当前仍是原版本
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": str(exc), "current": self.state.snapshot().as_dict()})
                return
            self._send_json(HTTPStatus.OK, snap.as_dict(), snap)
            return
        if path == "/admin/next":
            try:
                snap = self.state.next_version()
            except RuntimeError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except StatePersistError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": str(exc), "current": self.state.snapshot().as_dict()})
                return
            self._send_json(HTTPStatus.OK, snap.as_dict(), snap)
            return
        if path == "/admin/reload":
            try:
                snap = self.state.reload()
            except StatePersistError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": str(exc), "current": self.state.snapshot().as_dict()})
                return
            self._send_json(HTTPStatus.OK, snap.as_dict(), snap)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown admin path: {path}"})

    # ---- 静态内容 -------------------------------------------------
    def _serve_static(self, path: str) -> None:
        # 整个请求只取一次快照：响应头版号、HTML 占位符与正文目录
        # 全部来自同一个 (version, revision)，杜绝跨版混合响应
        snap = self.state.snapshot()
        if path == "/":
            path = "/index.html"
        # 防目录穿越：resolve 后必须仍在该快照的版本目录内
        base = (self.state.content_root / snap.version).resolve()
        target = (base / path.lstrip("/")).resolve()
        if base not in target.parents and target != base:
            self._send_text(HTTPStatus.FORBIDDEN, "forbidden", snap)
            return
        if not target.is_file():
            self._send_text(
                HTTPStatus.NOT_FOUND,
                f"页面不存在：{path}（站点版号 {snap.revision}，版本目录 {snap.version}）",
                snap,
            )
            return
        media = _MEDIA_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        if target.suffix in (".html", ".htm"):
            data = data.replace(b"__REVISION__", str(snap.revision).encode("ascii"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Site-Revision", str(snap.revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _send_text(self, status: int, text: str, snap: StateSnapshot) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Site-Revision", str(snap.revision))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)


def build_server(host: str = HOST, port: int = PORT,
                 state: SiteState | None = None) -> ThreadingHTTPServer:
    """构造服务器；可注入隔离状态（测试用）。"""
    active = state or get_state()

    class Handler(_Handler):
        # 每个服务器持有自己的状态实例，测试间互不串扰
        state = active

    server = ThreadingHTTPServer((host, port), Handler)
    snap = active.snapshot()
    print(f"[site] serving {active.content_root} "
          f"versions={list(snap.available_versions)} "
          f"current={snap.version} revision={snap.revision} "
          f"on {host}:{port}")
    return server


def main() -> None:
    server = build_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
