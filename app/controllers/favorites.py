"""controller：收藏同步（把站点账号全部收藏标为本地「待下载」）。"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from app.services import favorites_service


def post_favorites_sync(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    api_key = h._resolve_api_key(parse_qs(urlsplit(h.path).query), user)
    if not api_key:
        h._error("该接口需要 API Key", 401)
        return
    user_db = h._user_db_path(user)
    origin = h._site_origin(user)
    if not origin:
        h._error("未知站点地址：请先抓取一次列表页", 400)
        return
    h._json(favorites_service.sync_to_want(api_key, user, user_db, origin,
                                           upstream=h._upstream_cls()))
