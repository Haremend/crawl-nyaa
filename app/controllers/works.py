"""controller：作品域（meta 聚合 / 作品列表 / 批次 / 收藏列表 / 标记）。"""
from __future__ import annotations

from app import config, db
from app import supabase_client
from app.controllers.util import int_param, parse_works_filters
from app.services import cloud_service, works_service, favorites_service, sync_service


def get_meta(h) -> None:
    user = h._current_user()
    user_db = h._user_db_path(user)
    if config.storage_is_cloud():
        username = user.get("username", "") if user else ""
        batches = cloud_service.batches(username)
        stats = cloud_service.stats(username)
    else:
        batches = db.list_batches(user_db)
        stats = db.stats(user_db)
    h._json({
        "ok": True,
        "proxy": config.PROXY,
        "default_url": config.DEFAULT_URL,
        "db_path": str(user_db),
        "data_dir": str(config.DATA_DIR),
        "cover_dir": str(config.COVER_DIR),
        "download_dir": str(config.DOWNLOADS_DIR),
        "site_origin": h._site_origin(user),
        "download_mode": config.DOWNLOAD_MODE,
        "proxy_enabled": config.PROXY_ENABLED,
        "proxy_address": config.PROXY,
        "batches": batches,
        "download_status": h._filtered_jobs(user),
        "stats": stats,
        "limits": h._limits(),
        "scrape": h._scrape_service(user).snapshot(),
        "user": {
            "logged_in": bool(user),
            "username": user.get("username", "") if user else "",
            "has_api_key": bool(user.get("api_key")) if user else False,
            "site_origin": user.get("site_origin", "") if user else "",
        },
        "unlogin_user": config.UNLOGIN_USER,
        "is_unlogin_user": user is None,
        "supabase_configured": supabase_client.configured(),
        "storage_backend": "cloud" if config.storage_is_cloud() else "local",
        "remote_configured": config.remote_configured(),
        "remote_base_masked": sync_service.remote_base_masked(),
    })


def get_batches(h) -> None:
    user = h._current_user()
    if config.storage_is_cloud():
        h._json({"ok": True, "batches": cloud_service.batches(
            user.get("username", "") if user else "")})
    else:
        h._json({"ok": True, "batches": db.list_batches(h._user_db_path(user))})


def get_works(h, query) -> None:
    user = h._current_user()
    user_db = h._user_db_path(user)
    filters = parse_works_filters(query)
    try:
        data = works_service.list_works(filters, user, user_db)
    except supabase_client.SupabaseError as exc:
        h._error(str(exc), exc.status or 502)
        return
    if "error" in data:  # 云模式未登录兜底
        h._error(data.get("error", "云端模式必须先登录"), data.get("status", 401))
        return
    data["ok"] = True
    h._json(data)


def get_site_favorites(h, query) -> None:
    """站点账号收藏列表：用请求携带的 Key 拉取，并同步进本地库统一排序。"""
    user = h._current_user()
    api_key = h._resolve_api_key(query, user)
    if not api_key:
        h._error("该接口需要 API Key", 401)
        return
    user_db = h._user_db_path(user)
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    page = int_param(query, "page", 1, 1, 10000)
    keyword = (query.get("q", [""])[0] or "").strip()
    result = favorites_service.list_site_favorites(
        api_key, user, user_db, origin, page, keyword,
        upstream=h._upstream_cls())
    if not result.get("ok"):
        h._error(result.get("error", "收藏列表获取失败"), result.get("status", 502))
        return
    # items 是 gallery_id 列表（service 未装饰），controller 二次装饰
    result["items"] = h._ordered_by_ids(user, result["items"])
    h._json(result)


def _mark(h, flag: str, bool_key: str, setter) -> None:
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    value = bool(payload.get(bool_key, False))
    ids = payload.get("ids", [])
    if config.storage_is_cloud():
        updated = cloud_service.mark_flags(
            user.get("username", "") if user else "", flag, ids, value)
    else:
        updated = setter(ids, value, h._user_db_path(user))
    h._json({"ok": True, "updated": updated})


def post_want_download(h) -> None:
    _mark(h, "want_download", "want", db.set_want_download)


def post_hidden(h) -> None:
    _mark(h, "hidden", "hidden", db.set_hidden)


def post_downloaded(h) -> None:
    _mark(h, "downloaded", "downloaded", db.set_downloaded)
