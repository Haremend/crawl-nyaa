"""内置 HTTP 服务：静态前端 + JSON API（仅标准库）。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import config, covers as covers_mod, db
from .downloader import DownloadService
from .scraper import FetchError, Fetcher
from .upstream import UpstreamClient, UpstreamError
from .service import ScrapeError, ScrapeService


def _download_filename(gallery_id: int, fmt: str) -> str:
    """建议的下载文件名（前端 a[download] 用）。"""
    ext = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
    return f"{int(gallery_id)}.{ext}"


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
        self.download_service = DownloadService(self.db_path)
        # 启动时恢复上次保存的代理开关（云服务器上通常关闭代理直连）
        saved = db.get_setting("proxy_enabled", "", self.db_path)
        if saved != "":
            config.set_proxy_enabled(saved not in ("0", "false", "no"))


class Handler(BaseHTTPRequestHandler):
    server_version = "claw-gallery/0.1"
    protocol_version = "HTTP/1.1"
    quiet_paths = ("/api/scrape/status", "/api/thumb", "/static/")

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
                    items = db.list_batch_targets(batch, self.server.db_path)
                    self._json({"ok": True, "mode": "batch", "batch_id": batch,
                                "items": items, "total": len(items)})
                else:
                    items = db.list_want_download_targets(self.server.db_path)
                    self._json({"ok": True, "mode": "want", "items": items,
                                "total": len(items)})
            elif path == "/api/download/status":
                self._json({"ok": True, **self.server.download_service.snapshot()})
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
            "proxy_enabled": config.PROXY_ENABLED,
            "proxy_address": config.PROXY,
            "batches": db.list_batches(self.server.db_path),
            "download_status": self.server.download_service.snapshot(),
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

        self._json({
            "ok": True,
            "items": self._ordered_by_ids([item["gallery_id"] for item in items]),
            "page": payload.get("page", page),
            "num_pages": payload.get("num_pages", 1),
            "per_page": payload.get("per_page", 25),
            "total": payload.get("total", len(items)),
        })

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
                    )
                except ScrapeError as exc:
                    busy = "正在进行" in str(exc)
                    self._error(str(exc), 409 if busy else 400)
                    return
                self._json({"ok": True, "scrape": state})
            elif path == "/api/scrape/cancel":
                cancelled = self.server.service.cancel()
                self._json({"ok": True, "cancelled": cancelled, **self.server.service.snapshot()})
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
                try:
                    state = self.server.download_service.start(
                        api_key, str(payload.get("format") or "zip"),
                        mode="batch", batch_id=int(payload.get("batch_id") or 0))
                except UpstreamError as exc:
                    self._error(str(exc), exc.status or 400)
                    return
                self._json({"ok": True, "download": state})
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
                try:
                    state = self.server.download_service.start(
                        api_key, str(payload.get("format") or "zip"))
                except UpstreamError as exc:
                    self._error(str(exc), exc.status or 400)
                    return
                self._json({"ok": True, "download": state})
            elif path == "/api/download/cancel":
                self._json({"ok": True,
                            "cancelled": self.server.download_service.cancel(),
                            **self.server.download_service.snapshot()})
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