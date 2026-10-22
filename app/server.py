"""内置 HTTP 服务：静态前端 + JSON API（仅标准库）。

会话与多用户：
- 登录用户从 `claw_session` cookie 解析，业务数据读写该用户的独立 SQLite；
- 匿名（未登录）兼容模式继续使用默认库 + 浏览器侧 API Key 门禁；
- API Key 解析顺序：请求头 X-API-Key / ?apikey → 登录用户保存的 Key → 匿名门禁。
站点地址、API Key、下载任务都绑定当前用户；下载队列表是实例级共享，
但每条任务行带 username / origin，worker 据此找到用户库与站点地址。
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import config, covers as covers_mod, db
from . import supabase_client
from .services import works_service, download_service
from . import controllers
from .download_queue import DownloadQueue
from .scraper import Fetcher
from .upstream import UpstreamClient
from . import scrape_service as service_module
from .scrape_service import ScrapeError, ScrapeService


SESSION_COOKIE = "claw_session"


def _content_disposition(filename: str) -> str:
    """同时给出 ASCII 回退与 UTF-8 形式，兼容中文/日文标题。"""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


def normalize_origin(raw: str) -> str:
    """把用户填写的域名规范成 https://host[:port]；无效时返回空字符串。"""
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").strip().lower()
    if not host:
        return ""
    if "." not in host and host != "localhost":
        return ""
    scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    return f"{scheme}://{host}{port}"


def thumb_proxy_path(url: str) -> str:
    """把远端缩略图 URL 转成本地代理路径。"""
    if not url:
        return ""
    return "/api/thumb?u=" + quote(url, safe="")


def local_cover_path(filename: str) -> str:
    """本地已下载封面（cover/<id>.<ext>）的访问路径；无封面返回空串。"""
    name = (filename or "").strip()
    if not name or not covers_mod.is_safe_filename(name):
        return ""
    return "/covers/" + quote(name, safe="")


def is_media_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    return bool(parts.scheme in ('http', 'https') and parts.netloc)


def _parse_cookies(header: str | None) -> dict[str, str]:
    """从 Cookie 请求头解析成 dict（不依赖 http.cookies，保持轻量）。"""
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR（基类 HTTPServer 默认设的）语义比 Linux 宽松：允许
    # 多个进程同时绑同一端口，谁抢到连接谁应答 —— 会导致旧的 python run.py 实例
    # 叠在新实例下面、持续应答旧代码（登录 bug 排查时踩到，4 个旧进程同绑 8787）。
    # Windows 改用 SO_EXCLUSIVEADDRUSE：禁止别的进程再来绑同端口，本进程仍能
    # 绑过 TIME_WAIT 快速重启。Linux 的 SO_REUSEADDR 不允许多进程同绑（需
    # SO_REUSEPORT 且两侧都设），保持默认 True 即可。
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        # Windows：先设 SO_EXCLUSIVEADDRUSE（必须在 bind 前），再交给基类绑。
        # allow_reuse_address 在 Windows 为 False，基类不会再设 SO_REUSEADDR。
        if os.name == "nt":
            opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if opt is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
                except OSError:
                    pass
        return super().server_bind()

    def __init__(self, address, handler_cls, *, db_path=None, web_dir=None):
        super().__init__(address, handler_cls)
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.web_dir = Path(web_dir) if web_dir else config.WEB_DIR
        self.cover_dir = Path(config.COVER_DIR)
        # 默认账号库保存 users / sessions / download_jobs / 实例级 settings
        db.init_db(self.db_path)
        self.download_queue = DownloadQueue(self.db_path)
        self.download_queue.start()
        # 每个用户一个 ScrapeService（后台线程状态按用户隔离）
        self._scrape_services: dict[str, ScrapeService] = {}
        self._scrape_lock = threading.Lock()
        # 启动时恢复上次保存的代理开关（云服务器上通常关闭代理直连）
        saved = db.get_setting("proxy_enabled", "", self.db_path)
        if saved != "":
            config.set_proxy_enabled(saved not in ("0", "false", "no"))

    def service_for(self, username: str, db_path) -> ScrapeService:
        """按用户取/建抓取服务（匿名用户用空串作 key，落在默认库）。"""
        key = (username or "").strip()
        with self._scrape_lock:
            service = self._scrape_services.get(key)
            if service is None:
                db.init_db(db_path)
                service = ScrapeService(db_path)
                self._scrape_services[key] = service
            return service


class Handler(BaseHTTPRequestHandler):
    server_version = "claw-gallery/0.1"
    protocol_version = "HTTP/1.1"
    quiet_paths = ("/api/scrape/status", "/api/thumb", "/api/download/file", "/static/")

    def log_message(self, format, *args):  # noqa: A002 - 保持父类签名
        message = format % args
        if any(part in message for part in self.quiet_paths):
            return
        sys.stderr.write(f"[http] {self.address_string()} {message}\n")

    # ---------- 当前用户与 Key 解析 ----------
    def _current_user(self) -> dict | None:
        """从会话 cookie 解析当前登录用户；未登录返回 None。

        结果在单个请求内缓存，避免多次查库。
        """
        cached = getattr(self, "_user_cache", None)
        if cached is not None:
            return cached or None
        cookies = _parse_cookies(self.headers.get("Cookie"))
        token = cookies.get(SESSION_COOKIE, "")
        user = db.get_user_by_session(token, self.server.db_path) if token else None
        # 用哨兵 {} 区分「未登录」和「还没解析」
        self._user_cache = user or {}
        return user

    def _user_db_path(self, user: dict | None) -> Path:
        """当前用户的业务库路径；未登录用默认库。"""
        if user and user.get("username"):
            return db.user_db_path(user["username"])
        return self.server.db_path

    def _scrape_service(self, user: dict | None) -> ScrapeService:
        """按用户 + 存储模式取抓取服务。

        本地模式：SQLiteSink（写用户库 / 默认库，现状行为）；
        云模式：CloudSink（抓取数据直接写 Supabase，不落本地 SQLite）。
        """
        username = (user or {}).get("username", "")
        if config.storage_is_cloud():
            sink = service_module.CloudSink(username)
        else:
            sink = service_module.SQLiteSink(self._user_db_path(user))
        key = f"{config.STORAGE_BACKEND}:{username}"
        with self.server._scrape_lock:
            svc = self.server._scrape_services.get(key)
            if svc is None:
                svc = ScrapeService(sink=sink)
                self.server._scrape_services[key] = svc
            return svc

    def _resolve_api_key(self, query: dict | None, user: dict | None) -> str:
        """Key 解析顺序：请求头 X-API-Key → ?apikey= → 登录用户保存的 Key。"""
        header = (self.headers.get("X-API-Key") or "").strip()
        if header:
            return header
        if query:
            values = query.get("apikey") or []
            if values:
                return str(values[0]).strip()
        if user and user.get("api_key"):
            return str(user["api_key"])
        return ""

    def _site_origin(self, user: dict | None) -> str:
        """当前用户/匿名模式的站点地址。

        云模式：读云端账号行 claw_users.site_origin（注册时已写）；
        本地模式：读本地库 settings.site_origin。
        """
        if config.storage_is_cloud():
            # 云端账号行已含 site_origin（注册时写入）；读不到再查云端
            if user and user.get("site_origin"):
                return user["site_origin"].rstrip("/")
            if user:
                try:
                    acc = supabase_client.fetch_account(user["username"])
                    if acc and acc.get("site_origin"):
                        return str(acc["site_origin"]).rstrip("/")
                except supabase_client.SupabaseError:
                    return ""
            return ""
        return db.get_site_origin(self._user_db_path(user))

    def _ensure_login(self) -> dict | None:
        """要求登录；未登录返回 None 并发送 401。

        云端模式必须先登录才能用任何业务接口；本地模式允许匿名（门禁兼容）。
        """
        user = self._current_user()
        if not user:
            if config.storage_is_cloud():
                self._error("云端模式必须先登录", 401)
            else:
                self._error("请先登录", 401)
            return None
        return user

    # ---------- 响应工具 ----------
    def _send(self, body: bytes, content_type: str, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _json(self, payload: dict, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status,
                   {"Cache-Control": "no-store", **(headers or {})})

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            self._error("请求体过大", 413)
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error("请求体不是合法 JSON", 400)
            return None
        if not isinstance(payload, dict):
            self._error("请求体必须是 JSON 对象", 400)
            return None
        return payload

    def _session_cookie_header(self, token: str, *, max_age: int) -> str:
        """构造 Set-Cookie：HttpOnly + SameSite=Lax + Path=/。"""
        if token:
            return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"
        return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"

    # 云端模式：这些路径允许匿名访问（静态资源、认证接口、同步状态、机器间远程端点）
    # /api/remote/* 是给「其他 claw 实例」调的，不能要求浏览器登录态，因此公开路由；
    # 真正的访问控制靠 handler 里的 _require_remote_guard（Bearer 共享密钥）。
    _PUBLIC_PATHS = ("/", "/index.html", "/favicon.ico", "/static/", "/covers/",
                     "/api/auth/me", "/api/auth/register", "/api/auth/login",
                     "/api/auth/logout", "/api/sync/status", "/api/meta",
                     "/api/remote/account", "/api/remote/sync")

    def _require_remote_guard(self) -> bool:
        """/api/remote/* 的访问控制：B 共享密钥校验。

        配了 CLAW_REMOTE_AUTH_TOKEN 时，请求必须带匹配的
        ``Authorization: Bearer <token>``，否则 403；未配置则放行（开放）。
        返回 True 放行，False 表示已发 403、调用方应直接 return。
        """
        expected = config.REMOTE_AUTH_TOKEN
        if not expected:
            return True
        header = (self.headers.get("Authorization") or "").strip()
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not secrets.compare_digest(token, expected):
            self._error("远程端点密钥不匹配", 403)
            return False
        return True

    def _cloud_login_guard(self, path: str) -> bool:
        """云端模式强制登录：返回 True 表示已放行（或非云模式），
        False 表示应拒绝（已发 401）。"""
        if not config.storage_is_cloud():
            return True
        if any(path == p or path.startswith(p) for p in self._PUBLIC_PATHS):
            return True
        if not self._current_user():
            self._error("云端模式必须先登录", 401)
            return False
        return True

    # ---------- GET ----------
    def do_GET(self) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if not self._cloud_login_guard(path):
                return
            if path in ("/", "/index.html"):
                self._serve_file(self.server.web_dir / "index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path.startswith("/covers/"):
                self._serve_cover(path[len("/covers/"):])
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
            elif controllers.known("GET", path):
                controllers.dispatch(self, "GET", path, query)
            else:
                self._error("未知路径", 404)
        except Exception as exc:  # 防御性兜底，避免连接挂起
            import traceback
            traceback.print_exc()
            self._error(f"服务器内部错误：{type(exc).__name__}: {exc}", 500)

    def _filtered_jobs(self, user: dict | None, limit: int = 100) -> dict:
        """下载队列按当前用户过滤：登录用户只看自己的任务，匿名看全部。"""
        snapshot = self.server.download_queue.snapshot(limit)
        if not user or not user.get("username"):
            return snapshot
        username = user["username"]
        jobs = [job for job in snapshot.get("jobs", []) if (job.get("username") or "") == username]
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for job in jobs:
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        snapshot["jobs"] = jobs
        snapshot["counts"] = counts
        return snapshot

    def _serve_file(self, target: Path, cache: str = "no-store") -> None:
        try:
            body = target.read_bytes()
        except OSError:
            self._error(f"文件不存在：{target.name}", 404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._send(body, content_type, 200, {"Cache-Control": cache})

    def _serve_static(self, relative: str) -> None:
        web_root = self.server.web_dir.resolve()
        target = (web_root / relative).resolve()
        if target != web_root and web_root not in target.parents:
            self._error("非法路径", 403)
            return
        self._serve_file(target)

    def _serve_cover(self, relative: str) -> None:
        name = (relative or "").strip()
        if not covers_mod.is_safe_filename(name):
            self._error("非法封面文件名", 403)
            return
        target = (self.server.cover_dir / name).resolve()
        cover_root = self.server.cover_dir.resolve()
        if cover_root not in target.parents:
            self._error("非法路径", 403)
            return
        if not target.is_file():
            self._error("封面不存在", 404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "image/webp"
        self._send(target.read_bytes(), content_type, 200, {"Cache-Control": "public, max-age=604800"})

    def _limits(self) -> dict:
        """抓取表单的默认值与上下限；单一来源是 config（即 .env）。

        前端 applyLimits() 直接拿这些值初始化输入框/复选框，
        页面里不再硬编码，避免「改了 .env 页面没变」。
        """
        min_delay, max_delay = config.page_delay_bounds()
        return {
            "max_pages": config.MAX_PAGES,
            "default_delay": config.PAGE_DELAY,
            "min_delay": min_delay,
            "max_delay": max_delay,
            "detail_delay": config.DETAIL_DELAY,
            "cover_delay": config.COVER_DELAY,
            "enrich_default": config.ENRICH_DEFAULT,
            "covers_default": config.COVER_DEFAULT,
            "incremental_default": config.INCREMENTAL,
        }

    def _ordered_by_ids(self, user: dict | None, ids: list[int]) -> list[dict]:
        """按本地默认优先级规则，返回这些 id 对应的作品（含封面与标记）。"""
        wanted = {int(i) for i in ids if i}
        if not wanted:
            return []
        user_db = self._user_db_path(user)
        picked: list[dict] = []
        seen: set[int] = set()
        page = 1
        while True:
            data = db.query_works(page=page, per_page=200, sort="priority", path=user_db)
            for item in data["items"]:
                if wanted.intersection(item["ids"]) and item["gallery_id"] not in seen:
                    seen.add(item["gallery_id"])
                    picked.append(works_service._decorate(item))
            if page >= data.get("pages", 1):
                break
            page += 1
        return picked

    def _send_download_error(self, message: str, status: int = 400) -> None:
        """下载接口出错时返回一个简单页面（而不是让浏览器停在 JSON 上）。"""
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>下载失败</title>"
            "<body style=\"font-family:system-ui,'Microsoft YaHei';padding:40px;color:#14161a\">"
            f"<h2 style=\"color:#d6336c\">下载失败</h2><p>{message}</p>"
            "<p><a href=\"/\" style=\"color:#1f5fbd\">← 返回应用</a></p></body>"
        ).encode("utf-8")
        self._send(body, "text/html; charset=utf-8", status)

    def _send_local_file(self, path: Path, filename: str) -> None:
        """把服务器磁盘上的文件按块发给浏览器（同样不整块读进内存）。"""
        try:
            size = path.stat().st_size
        except OSError:
            self._send_download_error("缓存文件读取失败。", 500)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", download_service.content_disposition(filename))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            self.close_connection = True

    def _upstream_cls(self):
        """取当前模块作用域的 UpstreamClient（测试可 monkeypatch server 模块属性）。"""
        return UpstreamClient

    # ---------- POST ----------
    def do_POST(self) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if not self._cloud_login_guard(path):
                return
            if controllers.known("POST", path):
                controllers.dispatch(self, "POST", path, parse_qs(parts.query))
            else:
                self._error("未知接口", 404)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._error(f"服务器内部错误：{type(exc).__name__}: {exc}", 500)

    # ---------- 认证 ----------
def run_server(host: str | None = None, port: int | None = None, *,
               db_path=None, web_dir=None, open_browser: bool = False) -> None:
    host = host or config.HOST
    port = int(port if port is not None else config.PORT)
    server = ApiServer((host, port), Handler, db_path=db_path, web_dir=web_dir)
    url = f"http://{host}:{server.server_address[1]}/"
    print("claw-gallery 已启动：", url)
    print("SQLite 数据文件：", Path(server.db_path))
    effective = config.effective_proxy()
    print("抓取代理：", effective if effective else "直连（未使用代理）")
    print("按 Ctrl+C 停止服务")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        try:
            server.download_queue.stop()
        finally:
            try:
                server.shutdown()
            finally:
                server.server_close()
