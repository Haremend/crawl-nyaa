"""controller：下载域（文件转发 / 取 URL / 入队 / 取消 / 目标列表）。

HTTP 底层动作（_send_local_file / _send_download_error / 流式 wfile）留 Handler，
本层调 download_service 拿描述符/结果后格式化。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from app import config, db, supabase_client
from app.controllers.util import int_param, parse_works_filters
from app.services import download_service, works_service


def get_download_targets(h, query) -> None:
    """GET /api/download/targets：mode=want|batch|filter 的目标列表（供浏览器循环下载）。"""
    mode = (query.get("mode", ["want"])[0] or "want").lower()
    user = h._current_user()
    user_db = h._user_db_path(user)
    redownload = query.get("redownload", ["0"])[0] not in ("1", "true", "yes")
    skip = not redownload
    if mode == "batch":
        batch = int_param(query, "batch_id", 0, 0, 10 ** 13)
        if batch <= 0:
            h._error("缺少 batch_id", 400)
            return
        items = download_service.batch_targets(
            user, user_db, batch, cloud_mode=config.storage_is_cloud(),
            skip_downloaded=skip)
        h._json({"ok": True, "mode": "batch", "batch_id": batch,
                 "items": items, "total": len(items)})
    elif mode == "filter":
        # 与 /api/works 同名筛选参数（q/lang/dedupe/sort/uncensored/want/
        # downloaded/site_favorites/batch），前端 buildParams(false) 直接可拼
        filters = parse_works_filters(query)
        try:
            items = download_service.filter_targets(
                filters, user, user_db, cloud_mode=config.storage_is_cloud(),
                skip_downloaded=skip)
        except supabase_client.SupabaseError as exc:
            h._error(str(exc), exc.status or 502)
            return
        h._json({"ok": True, "mode": "filter", "items": items,
                 "total": len(items)})
    else:
        items = download_service.want_targets(
            user, user_db, cloud_mode=config.storage_is_cloud(),
            skip_downloaded=skip)
        h._json({"ok": True, "mode": "want", "items": items,
                 "total": len(items)})


def get_download_jobs(h, query) -> None:
    """GET /api/download/jobs：当前用户可见的下载队列。"""
    user = h._current_user()
    limit = int_param(query, "limit", 100, 1, 500)
    h._json({"ok": True, **h._filtered_jobs(user, limit)})


def get_download_status(h) -> None:
    """GET /api/download/status：下载队列快照（不含 limit 过滤）。"""
    user = h._current_user()
    h._json({"ok": True, **h._filtered_jobs(user)})


def get_download_file(h, query) -> None:
    """GET /api/download/file：转发作品文件（同源 + attachment）。"""
    user = h._current_user()
    api_key = h._resolve_api_key(query, user)
    if not api_key:
        h._send_download_error("需要 API Key：请先登录或在门禁里填写并保存。", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._send_download_error("未知站点地址：请先在设置里填写站点域名。", 400)
        return
    try:
        gallery_id = int((query.get("gallery_id", ["0"])[0] or 0))
    except (TypeError, ValueError):
        gallery_id = 0
    if gallery_id <= 0:
        h._send_download_error("缺少有效的 gallery_id。", 400)
        return
    fmt = (query.get("format", ["zip"])[0] or "zip").lower()
    if fmt not in ("zip", "cbz", "torrent"):
        fmt = "zip"
    username = user.get("username", "") if user else ""
    descriptor = download_service.serve_file(
        gallery_id, fmt, api_key=api_key, origin=origin,
        username=username, user_db=h._user_db_path(user),
        cloud_mode=config.storage_is_cloud())
    if descriptor["kind"] == "error":
        h._send_download_error(descriptor["message"], descriptor["status"])
        return
    if descriptor["kind"] == "local":
        h._send_local_file(descriptor["path"], descriptor["filename"])
        return
    # stream 模式：response 句柄由 service 已 open_stream，这里 64KB 边读边转发
    from app.scraper import FetchError
    response = descriptor["response"]
    filename = descriptor["filename"]
    try:
        content_type = descriptor["content_type"]
        length = descriptor["length"]
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Disposition", download_service.content_disposition(filename))
        if length:
            h.send_header("Content-Length", str(length))
        else:
            h.close_connection = True
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            h.wfile.write(chunk)
    except (OSError, FetchError):
        h.close_connection = True
    finally:
        try:
            response.close()
        except Exception:
            pass


def post_download_url(h) -> None:
    """POST /api/download-url：取单个作品签名下载地址（需 Key）。"""
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("该接口需要 API Key", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    try:
        gallery_id = int(payload.get("gallery_id") or 0)
    except (TypeError, ValueError):
        gallery_id = 0
    if gallery_id <= 0:
        h._error("缺少有效的 gallery_id", 400)
        return
    fmt = str(payload.get("format") or "zip")
    username = user.get("username", "") if user else ""
    result = download_service.fetch_download_url(
        gallery_id, fmt, api_key=api_key, origin=origin,
        username=username, user_db=h._user_db_path(user),
        cloud_mode=config.storage_is_cloud())
    if not result.get("ok"):
        h._error(result.get("error", "取下载地址失败"), result.get("status", 502))
        return
    h._json(result)


def post_download_enqueue(h) -> None:
    """POST /api/download/enqueue：单个作品手动入队。"""
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("缺少 API Key", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    try:
        gallery_id = int(payload.get("gallery_id") or 0)
    except (TypeError, ValueError):
        gallery_id = 0
    if gallery_id <= 0:
        h._error("缺少有效的 gallery_id", 400)
        return
    username = user.get("username", "") if user else ""
    job = h.server.download_queue.enqueue(
        gallery_id, str(payload.get("format") or "zip"),
        api_key=api_key, source="manual", username=username, origin=origin)
    h._json({"ok": True, "job": job,
             "queue": h._filtered_jobs(user)})


def post_batch_save(h) -> None:
    """POST /api/batch/save：保存全部（当前批次）到队列。"""
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("缺少 API Key", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    validation = download_service.validate_key(api_key, origin)
    if validation:
        h._error(validation["error"], validation["status"])
        return
    fmt = str(payload.get("format") or "zip")
    batch_id = int(payload.get("batch_id") or 0)
    redownload = bool(payload.get("redownload", False))
    if batch_id <= 0:
        h._error("请先选择一个批次", 400)
        return
    targets = download_service.batch_targets(
        user, h._user_db_path(user), batch_id,
        cloud_mode=config.storage_is_cloud(), skip_downloaded=not redownload)
    if not targets:
        h._error("该批次没有需要下载的作品（可勾选「重新下载」忽略已下载标记）", 400)
        return
    username = user.get("username", "") if user else ""
    result = h.server.download_queue.enqueue_many(
        targets, fmt, api_key=api_key, source="batch", batch_id=batch_id,
        username=username, origin=origin)
    h._json({"ok": True, "queued": result["queued"], "total": len(targets),
             "queue": h._filtered_jobs(user)})


def post_download_start(h) -> None:
    """POST /api/download/start：一键下载全部「待下载」。"""
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("缺少 API Key", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    validation = download_service.validate_key(api_key, origin)
    if validation:
        h._error(validation["error"], validation["status"])
        return
    fmt = str(payload.get("format") or "zip")
    redownload = bool(payload.get("redownload", False))
    targets = download_service.want_targets(
        user, h._user_db_path(user),
        cloud_mode=config.storage_is_cloud(), skip_downloaded=not redownload)
    if not targets:
        h._error("没有需要下载的作品（可勾选「重新下载」忽略已下载标记）", 400)
        return
    username = user.get("username", "") if user else ""
    result = h.server.download_queue.enqueue_many(
        targets, fmt, api_key=api_key, source="want", username=username, origin=origin)
    h._json({"ok": True, "queued": result["queued"], "total": len(targets),
             "queue": h._filtered_jobs(user)})


def normalize_works_filters(payload: dict | None) -> dict:
    """POST body 里的 filters（URLSearchParams 转对象）→ works filters dict。

    与 GET /api/works 的 query 参数同语义（q/lang/dedupe/sort/uncensored/
    want/downloaded/pending/site_favorites/batch），保证「下载当前筛选结果」
    与页面当前列表条件完全一致。
    """
    payload = payload or {}

    def as_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    query = {
        "q": as_list(payload.get("q")) or [""],
        "lang": as_list(payload.get("lang")),
        "dedupe": as_list(payload.get("dedupe", "1")),
        "sort": as_list(payload.get("sort", "recent")),
        "uncensored": as_list(payload.get("uncensored", "0")),
        "want": as_list(payload.get("want", "0")),
        "downloaded": as_list(payload.get("downloaded", "0")),
        "pending": as_list(payload.get("pending", "0")),
        "site_favorites": as_list(payload.get("site_favorites", "0")),
        "batch": as_list(payload.get("batch")),
    }
    return parse_works_filters(query)


def post_download_filter(h) -> None:
    """POST /api/download/filter：入队「当前筛选结果」的全部目标（推荐版本）。

    与 /api/download/start（待下载）、/api/batch/save（批次）对称：按当前
    筛选条件重查整份结果并入队，source="filter"。
    """
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("缺少 API Key", 401)
        return
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    validation = download_service.validate_key(api_key, origin)
    if validation:
        h._error(validation["error"], validation["status"])
        return
    fmt = str(payload.get("format") or "zip")
    redownload = bool(payload.get("redownload", False))
    filters = normalize_works_filters(payload.get("filters") or {})
    try:
        targets = download_service.filter_targets(
            filters, user, h._user_db_path(user),
            cloud_mode=config.storage_is_cloud(),
            skip_downloaded=not redownload)
    except supabase_client.SupabaseError as exc:
        h._error(str(exc), exc.status or 502)
        return
    if not targets:
        h._error("当前筛选条件下没有需要下载的作品（可勾选「重新下载」忽略已下载标记）", 400)
        return
    username = user.get("username", "") if user else ""
    result = h.server.download_queue.enqueue_many(
        targets, fmt, api_key=api_key, source="filter",
        username=username, origin=origin)
    h._json({"ok": True, "queued": result["queued"], "total": len(targets),
             "queue": h._filtered_jobs(user)})


def post_download_cancel(h) -> None:
    """POST /api/download/cancel：取消全部任务。"""
    info = h.server.download_queue.cancel_all()
    h._json({"ok": True, **info, **h.server.download_queue.snapshot()})


def post_download_jobs_cancel(h) -> None:
    """POST /api/download/jobs/cancel：取消单任务或当前用户全部排队任务。"""
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    if payload.get("all"):
        # 只取消当前用户的任务：逐个 cancel，避免误杀别人的队列
        snapshot = h.server.download_queue.snapshot()
        username = user.get("username", "") if user else ""
        count = 0
        for job in snapshot.get("jobs", []):
            if (job.get("username") or "") == username and job.get("status") == "queued":
                if h.server.download_queue.cancel(int(job["job_id"])).get("cancelled"):
                    count += 1
        info = {"cancelled_queued": count}
    else:
        try:
            job_id = int(payload.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        info = h.server.download_queue.cancel(job_id) if job_id else {"cancelled": False}
    h._json({"ok": True, **info, **h._filtered_jobs(user)})


def post_download_jobs_clear_failed(h) -> None:
    """POST /api/download/jobs/clear-failed：清除当前用户的 failed/cancelled 任务行。"""
    user = h._current_user()
    username = user.get("username", "") if user else ""
    removed = h.server.download_queue.clear_failed_jobs(username=username)
    h._json({"ok": True, "removed": removed})


def post_reset(h) -> None:
    """POST /api/reset：清空作品与批次（云/本地）。"""
    user = h._current_user()
    removed = works_service.reset(user, h._user_db_path(user))
    h._json({"ok": True, "removed": removed})
