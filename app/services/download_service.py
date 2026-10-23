"""下载业务逻辑：文件转发（cache/stream 双模式 + 落盘锁）、下载目标计算。

从 app/server.py 的 _handle_download_file / _find_cached_file / _ensure_cached_file /
_handle_download_url / _handle_batch_save / _handle_download_start 抽出。

分层约定：HTTP 底层动作（send_response / send_header / wfile / close_connection、
_send_local_file、_send_download_error 页面）**留 controller**；本层只做「算出
该转发什么 + 上游取签名 URL + 查缓存/落盘」，返回描述符 dict。

描述符 shape（controller 消费）：
- {"kind": "local", "path": Path, "filename": str}          → 读盘转发
- {"kind": "stream", "response": <urlopen resp>, "filename": str,
   "content_type": str, "length": int|None}                  → 64KB 边读边转
- {"kind": "error", "message": str, "status": int}          → 回下载错误页
"""
from __future__ import annotations

import mimetypes
import os
import threading
from pathlib import Path
from urllib.parse import quote

from app import config, db, supabase_client
from app.filenames import download_filename
from app.scraper import FetchError, Fetcher
from app.upstream import UpstreamClient, UpstreamError


# ---------- 同一作品的缓存下载锁：避免浏览器并发点击时重复请求上游 ----------
_CACHE_LOCKS: dict[int, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def cache_lock(gallery_id: int) -> threading.Lock:
    """取某个作品的缓存锁（不存在则创建）。"""
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(int(gallery_id))
        if lock is None:
            lock = threading.Lock()
            _CACHE_LOCKS[int(gallery_id)] = lock
        return lock


# ---------- 文件名 / 响应头 工具（controller 也需要 content_disposition）----------
def download_file_name(gallery_id: int, fmt: str, title: str = "") -> str:
    """下载文件名：{作品ID}_{作品名称}.{扩展名}（已做合规净化）。"""
    ext = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
    return download_filename(gallery_id, title, ext)


def content_disposition(filename: str) -> str:
    """同时给出 ASCII 回退与 UTF-8 形式，兼容中文/日文标题。"""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


# ---------- 云端标题（与 server._cloud_title 等价；不依赖 2b 的 cloud_service）----------
def cloud_title(username: str, gallery_id: int) -> str:
    """云模式取作品标题（title_pretty 优先，缺则 title）；取不到返回空串。"""
    username = (username or "").strip()
    if not username:
        return ""
    try:
        for r in supabase_client.cloud_fetch_gallery_rows(username):
            if int(r.get("gallery_id") or 0) == int(gallery_id):
                return (str(r.get("title_pretty") or "").strip()
                        or str(r.get("title") or "").strip())
    except supabase_client.SupabaseError:
        return ""
    return ""


def find_cached_file(gallery_id: int, fmt: str) -> Path | None:
    """在下载目录里找已缓存的文件（兼容 {id}_* 与历史 {id}.ext 命名）。"""
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


def ensure_cached_file(gallery_id: int, fmt: str, url: str, origin: str,
                       filename: str) -> Path | None:
    """cache 模式：确保文件已存在于服务器磁盘，返回文件路径（失败 None）。

    内部带锁 + 二次检查，避免同一作品并发重复下载；先写 .part 再原子改名。
    """
    cached = find_cached_file(gallery_id, fmt)
    if cached is not None:
        return cached

    cache_dir = Path(config.DOWNLOADS_DIR)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = cache_dir / filename

    with cache_lock(gallery_id):
        cached = find_cached_file(gallery_id, fmt)   # 双重检查
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


def serve_file(gallery_id: int, fmt: str, *, api_key: str, origin: str,
               username: str = "", user_db: Path | None = None,
               cloud_mode: bool = False) -> dict:
    """算出该转发什么：cache 查缓存/落盘、stream 开上游流。

    返回描述符 dict（见模块 docstring）；错误返回 {"kind":"error",...}。
    本层不碰 HTTP——local/stream 的写盘转发由 controller 完成。
    """
    title = (cloud_title(username, gallery_id) if cloud_mode
             else db.get_title(gallery_id, user_db))
    filename = download_file_name(gallery_id, fmt, title)

    # cache 模式：先查缓存。命中就直接读盘转发，完全不请求上游
    if config.DOWNLOAD_MODE == "cache":
        cached = find_cached_file(gallery_id, fmt)
        if cached is not None:
            return {"kind": "local", "path": cached, "filename": cached.name}

    try:
        info = UpstreamClient(origin, api_key).get_download_url(gallery_id, fmt)
    except UpstreamError as exc:
        return {"kind": "error", "message": f"取下载地址失败：{exc}",
                "status": exc.status or 502}
    url = str(info.get("url") or "")
    if not url:
        return {"kind": "error", "message": "上游未返回下载地址。", "status": 502}

    # cache 模式：落盘后再转发（内部有锁 + 二次检查，避免并发重复下载）
    if config.DOWNLOAD_MODE == "cache":
        cached = ensure_cached_file(gallery_id, fmt, url, origin, filename)
        if cached is None:
            return {"kind": "error",
                    "message": "下载失败：无法缓存到服务器磁盘。", "status": 502}
        return {"kind": "local", "path": cached, "filename": cached.name}

    # stream 模式：64KB 边读边转发（response 句柄交给 controller 流式写）
    fetcher = Fetcher()
    try:
        response = fetcher.open_stream(url, referer=f"{origin}/g/{gallery_id}/")
    except FetchError as exc:
        return {"kind": "error", "message": f"下载失败：{exc}", "status": 502}
    content_type = response.headers.get_content_type() or "application/octet-stream"
    length = response.headers.get("Content-Length")
    return {"kind": "stream", "response": response, "filename": filename,
            "content_type": content_type, "length": length}


def fetch_download_url(gallery_id: int, fmt: str, *, api_key: str, origin: str,
                       username: str = "", user_db: Path | None = None,
                       cloud_mode: bool = False) -> dict:
    """_handle_download_url 的业务体：取上游签名 URL + 拼本地文件名。

    返回 {"ok":True, url, expires_at, format, filename}；
    出错返回 {"ok":False, "error":str, "status":int}。
    """
    if not api_key:
        return {"ok": False, "error": "该接口需要 API Key", "status": 401}
    if not origin:
        return {"ok": False, "error": "未知站点地址：请先抓取一次列表页", "status": 400}
    if gallery_id <= 0:
        return {"ok": False, "error": "缺少有效的 gallery_id", "status": 400}
    try:
        info = UpstreamClient(origin, api_key).get_download_url(gallery_id, fmt)
    except UpstreamError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}
    if not info.get("url"):
        return {"ok": False, "error": "上游未返回下载地址", "status": 502}
    title = (cloud_title(username, gallery_id) if cloud_mode
             else db.get_title(gallery_id, user_db))
    return {"ok": True, "url": info.get("url"), "expires_at": info.get("expires_at"),
            "format": fmt, "filename": download_file_name(gallery_id, fmt, title)}


# ---------- 下载目标计算（cloud / local 双路）----------
def _cloud_want_targets(username: str, *, skip_downloaded: bool) -> list[dict]:
    """云模式：从云端作品行算「待下载」目标（组内任一 want_download 即命中，取推荐版本）。"""
    username = (username or "").strip()
    if not username:
        return []
    rows = supabase_client.cloud_fetch_gallery_rows(username)
    rows = [dict(r) for r in rows if not r.get("hidden")]
    if not rows:
        return []
    targets: list[dict] = []
    for group in db._cluster_rows(rows, True):
        if not any(r.get("want_download") for r in group):
            continue
        if skip_downloaded and any(r.get("downloaded") for r in group):
            continue
        ordered = sorted(group, key=lambda r: db._variant_rank(r, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main.get("title_pretty") or main.get("title") or ""),
        })
    targets.sort(key=lambda t: -t["gallery_id"])
    return targets


def _cloud_batch_targets(username: str, batch_id: int, *,
                         skip_downloaded: bool) -> list[dict]:
    """云模式：从云端作品行算指定批次目标。"""
    username = (username or "").strip()
    if not username:
        return []
    rows = [dict(r) for r in supabase_client.cloud_fetch_gallery_rows(username)
            if not r.get("hidden") and int(r.get("batch_id") or 0) == int(batch_id)]
    if not rows:
        return []
    targets: list[dict] = []
    for group in db._cluster_rows(rows, True):
        if skip_downloaded and any(r.get("downloaded") for r in group):
            continue
        ordered = sorted(group, key=lambda r: db._variant_rank(r, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main.get("title_pretty") or main.get("title") or ""),
        })
    targets.sort(key=lambda t: -t["gallery_id"])
    return targets


def want_targets(user: dict | None, user_db: Path | None,
                 *, cloud_mode: bool, skip_downloaded: bool) -> list[dict]:
    """「一键下载待下载」的目标列表；未登录（云模式无 username）返回空。"""
    username = (user.get("username", "") if user else "")
    if cloud_mode:
        return _cloud_want_targets(username, skip_downloaded=skip_downloaded)
    return db.list_want_download_targets(user_db, skip_downloaded=skip_downloaded)


def batch_targets(user: dict | None, user_db: Path | None, batch_id: int, *,
                  cloud_mode: bool, skip_downloaded: bool) -> list[dict]:
    """「保存全部（当前批次）」的目标列表。"""
    username = (user.get("username", "") if user else "")
    if cloud_mode:
        return _cloud_batch_targets(username, batch_id,
                                    skip_downloaded=skip_downloaded)
    return db.list_batch_targets(batch_id, user_db, skip_downloaded=skip_downloaded)


def filter_targets(filters: dict, user: dict | None, user_db,
                   *, cloud_mode: bool, skip_downloaded: bool) -> list[dict]:
    """「下载当前筛选结果」的目标列表：按 /api/works 同名筛选参数重查全部页，
    取每个去重组的推荐版本（item.gallery_id）作下载目标。

    云模式天然走 works_service.list_works → cloud_service.query_works；
    云模式无登录时 list_works 返回带 error 的结构，这里视作无目标返回空。
    """
    from app.services import works_service
    base = dict(filters or {})
    targets: list[dict] = []
    page = 1
    max_pages = 10000  # 防御性上限，避免异常 pages 字段导致死循环
    while page <= max_pages:
        f = dict(base)
        f["page"] = page
        f["per_page"] = 200
        data = works_service.list_works(f, user, user_db)
        if not isinstance(data, dict) or "items" not in data:
            break
        page_items = data.get("items") or []
        for item in page_items:
            if skip_downloaded and item.get("downloaded"):
                continue
            try:
                gid = int(item.get("gallery_id") or 0)
            except (TypeError, ValueError):
                continue
            if gid > 0:
                targets.append({"gallery_id": gid,
                                "title": str(item.get("title") or "")})
        try:
            pages = int(data.get("pages") or 1)
        except (TypeError, ValueError):
            pages = 1
        if not page_items or page >= pages:
            break
        page += 1
    return targets


def validate_key(api_key: str, origin: str) -> dict | None:
    """向上游 get_user 校验 Key；失败返回 {"error","status"}，成功返回 None。"""
    try:
        UpstreamClient(origin, api_key).get_user()
        return None
    except UpstreamError as exc:
        return {"error": f"API Key 校验失败：{exc}", "status": exc.status or 401}
