"""controller：设置域（凭据/站点/代理）。GET/POST 共用 credentials。"""
from __future__ import annotations

from app.services import settings_service


def get_credentials(h) -> None:
    user = h._ensure_login()
    if not user:
        return
    h._json(settings_service.get_credentials(user))


def post_credentials(h) -> None:
    user = h._ensure_login()
    if not user:
        return
    payload = h._read_json()
    if payload is None:
        return
    h._json(settings_service.save_credentials(
        user, payload.get("api_key"), payload.get("site_origin"),
        h.server.db_path))


def post_settings_site(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    result = settings_service.set_site_origin(
        user, payload.get("origin", ""), h.server.db_path,
        h._user_db_path(user))
    if not result.get("ok"):
        h._error(result.get("error", "域名格式不正确"), result.get("status", 400))
        return
    h._json(result)


def post_settings_proxy(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    enabled = bool(payload.get("enabled", True))
    h._json(settings_service.set_proxy(enabled, h.server.db_path))
