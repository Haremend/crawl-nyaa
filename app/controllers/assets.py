"""controller：静态资源代理 + 导出（缩略图代理 / CSV 导出）。HTTP 写留 Handler。"""
from __future__ import annotations

from app import config
from app.services import assets_service


def get_thumb(h, query) -> None:
    raw = (query.get("u", [""])[0] or "").strip()
    descriptor = assets_service.proxy_thumb(raw)
    if descriptor["kind"] == "error":
        h._error(descriptor["message"], descriptor["status"])
        return
    h._send(descriptor["body"], descriptor["mime"], 200, descriptor["cache_headers"])


def get_export_csv(h, query) -> None:
    user = h._current_user()
    user_db = h._user_db_path(user)
    q = (query.get("q", [""])[0] or "").strip() or None
    languages = [value.strip().lower() for value in query.get("lang", []) if value.strip()]
    dedupe = (query.get("dedupe", ["1"])[0] or "1").lower() not in ("0", "false", "no")
    sort = (query.get("sort", ["recent"])[0] or "recent").lower()
    if sort not in ("recent", "oldest", "title", "original", "variants", "favorites", "pages", "priority"):
        sort = "recent"
    uncensored = (query.get("uncensored", ["0"])[0] or "0").lower() not in ("0", "false", "no")
    want_only = (query.get("want", ["0"])[0] or "0").lower() not in ("0", "false", "no")
    site_only = (query.get("site_favorites", ["0"])[0] or "0").lower() not in ("0", "false", "no")
    params = {"q": q, "languages": languages, "dedupe": dedupe, "sort": sort,
              "uncensored": uncensored, "want": want_only, "site_favorites": site_only}
    body, headers = assets_service.export_csv(params, user, user_db,
                                              config.storage_is_cloud())
    h._send(body, "text/csv; charset=utf-8", 200, headers)
