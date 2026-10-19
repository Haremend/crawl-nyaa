"""内置 HTTP 服务：静态前端 + JSON API（仅标准库）。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import config, covers as covers_mod, db
from .download_queue import DownloadQueue
from .filenames import download_filename
from .scraper import FetchError, Fetcher
from .upstream import UpstreamClient, UpstreamError
from .service import ScrapeError, ScrapeService


# 同一作品的缓存下载锁，避免浏览器并发点击时重复请求上游
_CACHE_LOCKS: dict[int, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cache_lock(gallery_id: int) -> threading.Lock:
    """取某个作品的缓存锁（不存在则创建）。"""
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(int(gallery_id))
        if lock is None:
            lock = threading.Lock()
            _CACHE_LOCKS[int(gallery_id)] = lock
        return lock


def _download_filename(gallery_id: int, fmt: str, title: str = "") -> str:
    """下载文件名：{作品ID}_{作品名称}.{扩展名}（已做合规净化）。"""
    ext = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
    return download_filename(gallery_id, title, ext)


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


def _int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(query.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, *, db_path=None, web_dir=None):
        super().__init__(address, handler_cls)
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.web_dir = Path(web_dir) if web_dir else config.WEB_DIR
        self.cover_dir = Path(config.COVER_DIR)
        self.service = ScrapeService(self.db_path)
        self.download_queue = DownloadQueue(self.db_path)
        self.download_queue.start()
        # 启动时恢复上次保存的代理开关（云服务器上通常关闭代理直连）
        saved = db.get_setting("proxy_enabled", "", self.db_path)
        if saved != "":
            config.set_proxy_enabled(saved not in ("0", "false", "no"))


class Handler(BaseHTTPRequestHandler):
    server_version = "claw-gallery/0.1"
    protocol_version = "HTTP/1.1"
    quiet_paths = ("/api/scrape/status", "/api/thumb", "/api/download/file", "/static/")

    def log_message(self, format, *args):  # noqa: A002 - 保持父类签名
        message = format % args
        if any(part in message for part in self.quiet_paths):
            return
        sys.stderr.write(f"[http] {self.address_string()} {message}\n")

    def _api_key(self, query: dict | None = None) -> str:
        """从请求头 X-API-Key 或查询参数 apikey 读取用户 Key（不落盘、不记日志）。"""
        header = (self.headers.get("X-API-Key") or "").strip()
        if header:
            return header
        if query:
            values = query.get("apikey") or []
            if values:
                return str(values[0]).strip()
        return ""

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

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status, {"Cache-Control": "no-store"})

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

    # ---------- GET ----------
    def do_GET(self) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query)
            if path in ("/", "/index.html"):
                self._serve_file(self.server.web_dir / "index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path.startswith("/covers/"):
                self._serve_cover(path[len("/covers/"):])
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
            elif path == "/api/meta":
                self._handle_meta()
            elif path == "/api/works":
                self._handle_works(query)
            elif path == "/api/batches":
                self._json({"ok": True, "batches": db.list_batches(self.server.db_path)})
            elif path == "/api/site-favorites":
                self._handle_site_favorites(query)
            elif path == "/api/download/targets":
                mode = (query.get("mode", ["want"])[0] or "want").lower()
                if mode == "batch":
                    batch = _int_param(query, "batch_id", 0, 0, 10 ** 9)
                    if batch <= 0:
                        self._error("缺少 batch_id", 400)
                        return
                    items = db.list_batch_targets(
                        batch, self.server.db_path,
                        skip_downloaded=query.get("redownload", ["0"])[0] not in ("1", "true", "yes"))
                    self._json({"ok": True, "mode": "batch", "batch_id": batch,
                                "items": items, "total": len(items)})
                else:
                    items = db.list_want_download_targets(
                        self.server.db_path,
                        skip_downloaded=query.get("redownload", ["0"])[0] not in ("1", "true", "yes"))
                    self._json({"ok": True, "mode": "want", "items": items,
                                "total": len(items)})
            elif path == "/api/download/jobs":
                limit = _int_param(query, "limit", 100, 1, 500)
                self._json({"ok": True, **self.server.download_queue.snapshot(limit)})
            elif path == "/api/download/file":
                self._handle_download_file(query)
            elif path == "/api/download/status":
                self._json({"ok": True, **self.server.download_queue.snapshot()})
            elif path == "/api/scrape/status":
                self._json({"ok": True, **self.server.service.snapshot()})
            elif path == "/api/thumb":
                self._handle_thumb(query)
            elif path == "/api/export.csv":
                self._handle_export(query)
            else:
                self._error("未知路径", 404)
        except Exception as exc:  # 防御性兜底，避免连接挂起
            self._error(f"服务器内部错误：{type(exc).__name__}: {exc}", 500)

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

    def _handle_meta(self) -> None:
        self._json({
            "ok": True,
            "proxy": config.PROXY,
            "default_url": config.DEFAULT_URL,
            "db_path": str(self.server.db_path),
            "data_dir": str(config.DATA_DIR),
            "cover_dir": str(config.COVER_DIR),
            "download_dir": str(config.DOWNLOADS_DIR),
            "site_origin": db.get_site_origin(self.server.db_path),
            "download_mode": config.DOWNLOAD_MODE,
            "proxy_enabled": config.PROXY_ENABLED,
            "proxy_address": config.PROXY,
            "batches": db.list_batches(self.server.db_path),
            "download_status": self.server.download_queue.snapshot(),
            "stats": db.stats(self.server.db_path),
            "limits": {
                "max_pages": config.MAX_PAGES,
                "default_delay": config.PAGE_DELAY,
                "min_delay": config.MIN_PAGE_DELAY,
                "detail_delay": config.DETAIL_DELAY,
                "enrich_default": config.ENRICH_DEFAULT,
            },
            "scrape": self.server.service.snapshot(),
        })

    def _handle_works(self, query: dict) -> None:
        q = (query.get("q", [""])[0] or "").strip() or None
        languages = [value.strip().lower() for value in query.get("lang", []) if value.strip()]
        dedupe = (query.get("dedupe", ["1"])[0] or "1").lower() not in ("0", "false", "no")
        favorite_only = (query.get("favorite", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        sort = (query.get("sort", ["recent"])[0] or "recent").lower()
        if sort not in ("recent", "oldest", "title", "original", "variants", "favorites", "pages", "priority"):
            sort = "recent"
        uncensored = (query.get("uncensored", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        want_only = (query.get("want", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        downloaded_only = (query.get("downloaded", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        site_only = (query.get("site_favorites", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        batch_id = _int_param(query, "batch", 0, 0, 10 ** 9) or None
        page = _int_param(query, "page", 1, 1, 100000)
        per_page = _int_param(query, "per_page", 24, 1, 200)
        data = db.query_works(
            q=q, languages=languages, dedupe_title=dedupe, sort=sort,
            page=page, per_page=per_page, uncensored=uncensored,
            want_download_only=want_only, downloaded_only=downloaded_only,
            site_favorites_only=site_only, batch_id=batch_id, path=self.server.db_path,
        )
        for item in data["items"]:
            self._decorate(item)
        data["ok"] = True
        self._json(data)

    def _decorate(self, item: dict) -> dict:
        """给作品补上缩略图代理路径与本地封面路径。"""
        item["thumb"] = thumb_proxy_path(item.get("thumbnail") or "")
        item["cover_url"] = local_cover_path(item.get("cover_file") or "")
        for variant in item.get("variants", []):
            variant["thumb"] = thumb_proxy_path(variant.get("thumbnail") or "")
            variant["cover_url"] = local_cover_path(variant.get("cover_file") or "")
        return item

    def _ordered_by_ids(self, ids: list[int]) -> list[dict]:
        """按本地默认优先级规则，返回这些 id 对应的作品（含封面与标记）。"""
        wanted = {int(i) for i in ids if i}
        if not wanted:
            return []
        picked: list[dict] = []
        seen: set[int] = set()
        page = 1
        while True:
            data = db.query_works(page=page, per_page=200, sort="priority", path=self.server.db_path)
            for item in data["items"]:
                if wanted.intersection(item["ids"]) and item["gallery_id"] not in seen:
                    seen.add(item["gallery_id"])
                    picked.append(self._decorate(item))
            if page >= data.get("pages", 1):
                break
            page += 1
        return picked

    def _handle_site_favorites(self, query: dict) -> None:
        """站点账号收藏列表：用请求携带的 Key 拉取，并同步进本地库统一排序。"""
        api_key = self._api_key(query)
        if not api_key:
            self._error("该接口需要 API Key", 401)
            return
        origin = db.get_site_origin(self.server.db_path)
        if not origin:
            self._error("未知站点地址：请先抓取一次列表页", 400)
            return
        page = _int_param(query, "page", 1, 1, 10000)
        keyword = (query.get("q", [""])[0] or "").strip()
        try:
            payload = UpstreamClient(origin, api_key).list_favorites(page=page, query=keyword)
        except UpstreamError as exc:
            self._error(str(exc), exc.status or 502)
            return

        items = payload.get("items") or []
        for item in items:
            item["site_favorite"] = 1
            item["from_scrape"] = 0
            item["url"] = f"{origin}/g/{item['gallery_id']}/"
        if items:
            # 同步进本地库，这样「待下载/已下载」标记与排序都能复用同一套逻辑
            db.upsert_items(items, source_url=f"{origin}/favorites", path=self.server.db_path)
            db.set_site_favorite([item["gallery_id"] for item in items], True, self.server.db_path)

        self._json({
            "ok": True,
            "items": self._ordered_by_ids([item["gallery_id"] for item in items]),
            "page": payload.get("page", page),
            "num_pages": payload.get("num_pages", 1),
            "per_page": payload.get("per_page", 25),
            "total": payload.get("total", len(items)),
        })

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

    def _handle_download_file(self, query: dict) -> None:
        """把作品文件转发给浏览器（同源 + attachment）。

        为什么不直接把上游下载地址交给浏览器：
        1. 上游是签名短链，且与「取签名时的出口 IP/会话」绑定；后端若走代理取签名，
           浏览器直连必然报 `Invalid signature`；
        2. 跨域地址上的 download 属性会被浏览器忽略，点击会变成页面跳转。
        因此由后端取签名并流式转发，浏览器只跟本服务打交道。
        """
        api_key = self._api_key(query)
        if not api_key:
            self._send_download_error("需要 API Key：请先在页面门禁里填写并保存。", 401)
            return
        origin = db.get_site_origin(self.server.db_path)
        if not origin:
            self._send_download_error("未知站点地址：请先在门禁里填写站点域名。", 400)
            return
        try:
            gallery_id = int((query.get("gallery_id", ["0"])[0] or 0))
        except (TypeError, ValueError):
            gallery_id = 0
        if gallery_id <= 0:
            self._send_download_error("缺少有效的 gallery_id。", 400)
            return
        fmt = (query.get("format", ["zip"])[0] or "zip").lower()
        if fmt not in ("zip", "cbz", "torrent"):
            fmt = "zip"

        title = db.get_title(gallery_id, self.server.db_path)
        filename = _download_filename(gallery_id, fmt, title)

        # cache 模式：先查缓存。命中就直接读盘转发，完全不请求上游
        if config.DOWNLOAD_MODE == "cache":
            cached = self._find_cached_file(gallery_id, fmt)
            if cached is not None:
                self._send_local_file(cached, cached.name)
                return

        try:
            info = UpstreamClient(origin, api_key).get_download_url(gallery_id, fmt)
        except UpstreamError as exc:
            self._send_download_error(f"取下载地址失败：{exc}", exc.status or 502)
            return
        url = str(info.get("url") or "")
        if not url:
            self._send_download_error("上游未返回下载地址。", 502)
            return

        # cache 模式：落盘后再转发（内部有锁 + 二次检查，避免并发重复下载）
        if config.DOWNLOAD_MODE == "cache":
            cached = self._ensure_cached_file(gallery_id, fmt, url, origin, filename)
            if cached is None:
                self._send_download_error("下载失败：无法缓存到服务器磁盘。", 502)
                return
            self._send_local_file(cached, cached.name)
            return

        # stream 模式：64KB 边读边转发，不在服务器落盘
        fetcher = Fetcher()
        try:
            response = fetcher.open_stream(url, referer=f"{origin}/g/{gallery_id}/")
        except FetchError as exc:
            self._send_download_error(f"下载失败：{exc}", 502)
            return
        try:
            content_type = response.headers.get_content_type() or "application/octet-stream"
            length = response.headers.get("Content-Length")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", _content_disposition(filename))
            if length:
                self.send_header("Content-Length", str(length))
            else:
                # 长度未知：靠关闭连接告诉浏览器下载结束
                self.close_connection = True
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, FetchError):
            self.close_connection = True
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _find_cached_file(self, gallery_id: int, fmt: str):
        """在下载目录里找已缓存的文件（兼容 hhh 与历史命名）。"""
        cache_dir = Path(config.DOWNLOADS_DIR)
        if not cache_dir.is_dir():
            return None
        ext = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
        candidates = [f for f in cache_dir.glob(f"{int(gallery_id)}_*.{ext}")
                      if f.is_file() and f.stat().st_size > 0]
        legacy = cache_dir / f"{int(gallery_id)}.{ext}"
        if legacy.is_file() and legacy.stat().st_size > 0:
            candidates.append(legacy)
        if not candidates:
            return None
        return max(candidates, key=lambda f: f.stat().st_mtime)

    def _ensure_cached_file(self, gallery_id: int, fmt: str, url: str, origin: str, filename: str):
        """cache 模式：确保文件已存在于服务器磁盘，返回文件路径。

        已存在则直接复用；不存在则边下边写盘（先写 .part 再原子改名），
        同一作品并发点击时用锁保证只下载一次。
        """
        cached = self._find_cached_file(gallery_id, fmt)
        if cached is not None:
            return cached

        cache_dir = Path(config.DOWNLOADS_DIR)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        target = cache_dir / filename

        with _cache_lock(gallery_id):
            cached = self._find_cached_file(gallery_id, fmt)   # 双重检查
            if cached is not None:
                return cached
            temp = target.with_name(target.name + ".part")
            fetcher = Fetcher()
            try:
                response = fetcher.open_stream(url, referer=f"{origin}/g/{gallery_id}/")
            except FetchError:
                return None
            try:
                with open(temp, "wb") as fh:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
            except (OSError, FetchError):
                try:
                    temp.unlink()
                except OSError:
                    pass
                return None
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                if temp.stat().st_size <= 0:
                    temp.unlink()
                    return None
                os.replace(temp, target)          # 原子替换，避免半成品被读到
            except OSError:
                return None
            return target

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
        self.send_header("Content-Disposition", _content_disposition(filename))
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

    def _handle_thumb(self, query: dict) -> None:
        raw = (query.get("u", [""])[0] or "").strip()
        if not is_media_url(raw):
            self._error("只允许 http/https 图片地址", 400)
            return
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        cache_path = config.THUMB_CACHE_DIR / f"{digest}.bin"
        meta_path = config.THUMB_CACHE_DIR / f"{digest}.json"
        if cache_path.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self._send(
                    cache_path.read_bytes(),
                    meta.get("content_type") or "image/jpeg",
                    200,
                    {"Cache-Control": "public, max-age=86400"},
                )
                return
            except (OSError, ValueError):
                pass
        try:
            body, content_type = Fetcher().fetch_bytes(raw, referer=None)
        except FetchError as exc:
            self._error(str(exc), 502)
            return
        if len(body) > config.THUMB_MAX_BYTES:
            self._error("图片超过大小上限", 413)
            return
        try:
            config.THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(body)
            meta_path.write_text(
                json.dumps({"content_type": content_type, "url": raw}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._send(body, content_type, 200, {"Cache-Control": "public, max-age=86400"})

    def _handle_export(self, query: dict) -> None:
        q = (query.get("q", [""])[0] or "").strip() or None
        languages = [value.strip().lower() for value in query.get("lang", []) if value.strip()]
        dedupe = (query.get("dedupe", ["1"])[0] or "1").lower() not in ("0", "false", "no")
        sort = (query.get("sort", ["recent"])[0] or "recent").lower()
        if sort not in ("recent", "oldest", "title", "original", "variants", "favorites", "pages", "priority"):
            sort = "recent"
        uncensored = (query.get("uncensored", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        want_only = (query.get("want", ["0"])[0] or "0").lower() not in ("0", "false", "no")
        site_only = (query.get("site_favorites", ["0"])[0] or "0").lower() not in ("0", "false", "no")

        items: list[dict] = []
        page = 1
        while len(items) < config.EXPORT_MAX_ROWS:
            data = db.query_works(
                q=q, languages=languages, dedupe_title=dedupe, sort=sort,
                page=page, per_page=200, uncensored=uncensored,
                want_download_only=want_only, site_favorites_only=site_only,
                path=self.server.db_path,
            )
            items.extend(data["items"])
            if page >= data["pages"]:
                break
            page += 1

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["gallery_id", "all_variant_ids", "title", "title_original", "cover_file", "uncensored",
                         "languages", "artists", "groups", "parodies", "tags",
                         "num_pages", "num_favorites", "upload_date", "scanlator",
                         "url", "first_seen", "last_seen"])
        for item in items:
            writer.writerow([
                item["gallery_id"],
                " ".join(str(value) for value in item["ids"]),
                item["title"],
                item["title_original"],
                item["cover_file"],
                1 if item["uncensored"] else 0,
                " ".join(lang["key"] for lang in item["languages"]),
                " ".join(item["artists"]),
                " ".join(item["groups"]),
                " ".join(item["parodies"]),
                " ".join(item["tags"]),
                item["num_pages"] if item["num_pages"] is not None else "",
                item["num_favorites"] if item["num_favorites"] is not None else "",
                item["upload_date"],
                item["scanlator"],
                item["url"],
                item["first_seen"],
                item["last_seen"],
            ])
        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        self._send(body, "text/csv; charset=utf-8", 200, {
            "Content-Disposition": 'attachment; filename="gallery_works.csv"',
            "Cache-Control": "no-store",
        })

    # ---------- POST ----------
    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path == "/api/scrape":
                payload = self._read_json()
                if payload is None:
                    return
                try:
                    state = self.server.service.start(
                        payload.get("url", ""),
                        max_pages=payload.get("max_pages", 0),
                        delay=payload.get("delay"),
                        enrich=payload.get("enrich"),
                        covers=payload.get("covers"),
                        incremental=payload.get("incremental"),
                    )
                except ScrapeError as exc:
                    busy = "正在进行" in str(exc)
                    self._error(str(exc), 409 if busy else 400)
                    return
                self._json({"ok": True, "scrape": state})
            elif path == "/api/scrape/cancel":
                cancelled = self.server.service.cancel()
                self._json({"ok": True, "cancelled": cancelled, **self.server.service.snapshot()})
            elif path == "/api/settings/site":
                payload = self._read_json()
                if payload is None:
                    return
                origin = normalize_origin(str(payload.get("origin") or ""))
                if not origin:
                    self._error("域名格式不正确，例如 example.com", 400)
                    return
                db.set_setting("site_origin", origin, self.server.db_path)
                self._json({"ok": True, "site_origin": origin})
            elif path == "/api/settings/proxy":
                payload = self._read_json()
                if payload is None:
                    return
                enabled = bool(payload.get("enabled", True))
                config.set_proxy_enabled(enabled)
                db.set_setting("proxy_enabled", "1" if enabled else "0", self.server.db_path)
                self._json({"ok": True, "proxy_enabled": config.PROXY_ENABLED,
                            "proxy_address": config.PROXY})
            elif path == "/api/want-download":
                payload = self._read_json()
                if payload is None:
                    return
                updated = db.set_want_download(payload.get("ids", []),
                                               bool(payload.get("want", False)),
                                               self.server.db_path)
                self._json({"ok": True, "updated": updated})
            elif path == "/api/hidden":
                payload = self._read_json()
                if payload is None:
                    return
                updated = db.set_hidden(payload.get("ids", []),
                                        bool(payload.get("hidden", False)),
                                        self.server.db_path)
                self._json({"ok": True, "updated": updated})
            elif path == "/api/favorites/sync":
                payload = self._read_json()
                if payload is None:
                    return
                api_key = self._api_key(parse_qs(urlsplit(self.path).query))
                if not api_key:
                    self._error("该接口需要 API Key", 401)
                    return
                origin = db.get_site_origin(self.server.db_path)
                if not origin:
                    self._error("未知站点地址：请先抓取一次列表页", 400)
                    return
                client = UpstreamClient(origin, api_key)
                # 遍历收藏所有页，把全部收藏作品在本地标记为「待下载」
                synced = 0
                page = 1
                while True:
                    payload_data = client.list_favorites(page=page)
                    items = payload_data.get("items") or []
                    if not items:
                        break
                    for item in items:
                        item["site_favorite"] = 1
                        item["from_scrape"] = 0
                        item["url"] = f"{origin}/g/{item['gallery_id']}/"
                    if items:
                        ids = [item["gallery_id"] for item in items]
                        db.upsert_items(items, source_url=f"{origin}/favorites", path=self.server.db_path)
                        db.set_site_favorite(ids, True, self.server.db_path)
                    synced += db.set_want_download([item["gallery_id"] for item in items], True, self.server.db_path)
                    num_pages = int(payload_data.get("num_pages") or 1)
                    if page >= num_pages:
                        break
                    page += 1
                self._json({"ok": True, "synced": synced, "pages": page})
            elif path == "/api/downloaded":
                payload = self._read_json()
                if payload is None:
                    return
                updated = db.set_downloaded(payload.get("ids", []),
                                            bool(payload.get("downloaded", False)),
                                            self.server.db_path)
                self._json({"ok": True, "updated": updated})
            elif path == "/api/download-url":
                # 只取下载地址返回给浏览器，由用户自己的网络下载（不在服务器落盘）
                payload = self._read_json()
                if payload is None:
                    return
                api_key = self._api_key(parse_qs(urlsplit(self.path).query))
                if not api_key:
                    self._error("该接口需要 API Key", 401)
                    return
                origin = db.get_site_origin(self.server.db_path)
                if not origin:
                    self._error("未知站点地址：请先抓取一次列表页", 400)
                    return
                try:
                    gallery_id = int(payload.get("gallery_id") or 0)
                except (TypeError, ValueError):
                    gallery_id = 0
                if gallery_id <= 0:
                    self._error("缺少有效的 gallery_id", 400)
                    return
                fmt = str(payload.get("format") or "zip")
                try:
                    info = UpstreamClient(origin, api_key).get_download_url(gallery_id, fmt)
                except UpstreamError as exc:
                    self._error(str(exc), exc.status or 502)
                    return
                if not info.get("url"):
                    self._error("上游未返回下载地址", 502)
                    return
                self._json({"ok": True, "url": info.get("url"),
                            "expires_at": info.get("expires_at"), "format": fmt,
                            "filename": _download_filename(gallery_id, fmt)})
            elif path == "/api/batch/save":
                # 保存当前批次的全部数据到 download/batch_<id>/
                payload = self._read_json()
                if payload is None:
                    return
                api_key = self._api_key(parse_qs(urlsplit(self.path).query))
                if not api_key:
                    self._error("缺少 API Key", 401)
                    return
                origin = db.get_site_origin(self.server.db_path)
                if not origin:
                    self._error("未知站点地址：请先抓取一次列表页", 400)
                    return
                try:
                    UpstreamClient(origin, api_key).get_user()
                except UpstreamError as exc:
                    self._error(f"API Key 校验失败：{exc}", exc.status or 401)
                    return
                fmt = str(payload.get("format") or "zip")
                batch_id = int(payload.get("batch_id") or 0)
                redownload = bool(payload.get("redownload", False))
                if batch_id <= 0:
                    self._error("请先选择一个批次", 400)
                    return
                targets = db.list_batch_targets(self.server.db_path, batch_id,
                                                skip_downloaded=not redownload)
                if not targets:
                    self._error("该批次没有需要下载的作品（可勾选「重新下载」忽略已下载标记）", 400)
                    return
                result = self.server.download_queue.enqueue_many(
                    targets, fmt, api_key=api_key, source="batch", batch_id=batch_id)
                self._json({"ok": True, "queued": result["queued"], "total": result["total"],
                            "queue": self.server.download_queue.snapshot()})
            elif path == "/api/download/start":
                payload = self._read_json()
                if payload is None:
                    return
                api_key = self._api_key(parse_qs(urlsplit(self.path).query))
                if not api_key:
                    self._error("缺少 API Key", 401)
                    return
                origin = db.get_site_origin(self.server.db_path)
                if not origin:
                    self._error("未知站点地址：请先抓取一次列表页", 400)
                    return
                # 先校验 Key，避免带着无效 Key 启动注定失败的任务
                try:
                    UpstreamClient(origin, api_key).get_user()
                except UpstreamError as exc:
                    self._error(f"API Key 校验失败：{exc}", exc.status or 401)
                    return
                fmt = str(payload.get("format") or "zip")
                redownload = bool(payload.get("redownload", False))
                targets = db.list_want_download_targets(
                    self.server.db_path, skip_downloaded=not redownload)
                if not targets:
                    self._error("没有需要下载的作品（可勾选「重新下载」忽略已下载标记）", 400)
                    return
                result = self.server.download_queue.enqueue_many(
                    targets, fmt, api_key=api_key, source="want")
                self._json({"ok": True, "queued": result["queued"], "total": result["total"],
                            "queue": self.server.download_queue.snapshot()})
            elif path == "/api/download/cancel":
                info = self.server.download_queue.cancel_all()
                self._json({"ok": True, **info, **self.server.download_queue.snapshot()})
            elif path == "/api/download/enqueue":
                # 单个作品入队（立即下载按钮）：任务在服务端跑，进度对所有页面可见
                payload = self._read_json()
                if payload is None:
                    return
                api_key = self._api_key(parse_qs(urlsplit(self.path).query))
                if not api_key:
                    self._error("缺少 API Key", 401)
                    return
                try:
                    gallery_id = int(payload.get("gallery_id") or 0)
                except (TypeError, ValueError):
                    gallery_id = 0
                if gallery_id <= 0:
                    self._error("缺少有效的 gallery_id", 400)
                    return
                job = self.server.download_queue.enqueue(
                    gallery_id, str(payload.get("format") or "zip"),
                    api_key=api_key, source="manual")
                self._json({"ok": True, "job": job,
                            "queue": self.server.download_queue.snapshot()})
            elif path == "/api/download/jobs/cancel":
                payload = self._read_json()
                if payload is None:
                    return
                if payload.get("all"):
                    info = self.server.download_queue.cancel_all()
                else:
                    try:
                        job_id = int(payload.get("job_id") or 0)
                    except (TypeError, ValueError):
                        job_id = 0
                    info = self.server.download_queue.cancel(job_id) if job_id else {"cancelled": False}
                self._json({"ok": True, **info, **self.server.download_queue.snapshot()})
            elif path == "/api/reset":
                removed = db.reset_all(self.server.db_path)
                self._json({"ok": True, "removed": removed})
            else:
                self._error("未知接口", 404)
        except Exception as exc:
            self._error(f"服务器内部错误：{type(exc).__name__}: {exc}", 500)


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
            server.shutdown()
        finally:
            server.server_close()