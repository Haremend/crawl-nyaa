"""settings 业务逻辑：凭据读/写、站点域名、代理开关（云/本地双写分支集中）。

从 app/server.py 的 _handle_credentials / _handle_settings_site /
_handle_settings_proxy 抽出。controller 负责登录门禁（_ensure_login）与 HTTP
格式化；本层做「云/本地往哪写」的分支决策与状态组装。行为与拆分前一致。
"""
from __future__ import annotations

from app import config, db, supabase_client
from app.services.auth_service import normalize_origin
from app.services._log import cloud_log


def get_credentials(user: dict) -> dict:
    """读当前用户凭据状态（不回显完整 api_key）。"""
    username = user["username"]
    return {"ok": True, "username": username,
            "has_api_key": bool(user.get("api_key")),
            "site_origin": user.get("site_origin", "")}


def save_credentials(user: dict, api_key, site_origin, db_path) -> dict:
    """保存站点域名 / API Key：本地账号库 + 用户库 settings，best-effort 推云端。

    api_key/site_origin 为 None 表示本次不更新该字段。
    """
    username = user["username"]
    origin = normalize_origin(str(site_origin)) if site_origin is not None else None
    db.update_user_settings(
        username,
        api_key=str(api_key).strip() if api_key is not None else None,
        site_origin=origin, path=db_path)
    if origin:
        db.set_setting("site_origin", origin, db.user_db_path(username))
    fresh = db.get_user(username, db_path) or {}
    cloud_pushed = False
    cloud_error = ""
    if supabase_client.configured():
        try:
            cloud_log(f"保存凭据→Supabase user={username}")
            supabase_client.ensure_tables()
            supabase_client.push_user_credentials(
                username, api_key=str(fresh.get("api_key") or ""),
                site_origin=str(fresh.get("site_origin") or ""),
                password_hash=str(fresh.get("password_hash") or "") if fresh.get("password_hash") else "",
                salt=str(fresh.get("salt") or "") if fresh.get("salt") else "",
                created_at=str(fresh.get("created_at") or ""),
            )
            cloud_pushed = True
        except supabase_client.SupabaseError as exc:
            # 云端不可用时不阻断本地保存
            cloud_error = str(exc)
    return {"ok": True, "username": username,
            "has_api_key": bool(fresh.get("api_key")),
            "site_origin": fresh.get("site_origin", ""),
            "cloud_pushed": cloud_pushed, "cloud_error": cloud_error}


def set_site_origin(user: dict | None, raw_origin: str, db_path,
                   user_db_path) -> dict:
    """设置站点域名：云模式写云端账号行；本地模式写用户库 + 账号库。"""
    origin = normalize_origin(str(raw_origin))
    if not origin:
        return {"ok": False, "error": "域名格式不正确，例如 example.com", "status": 400}
    username = user.get("username", "") if user else ""
    if config.storage_is_cloud():
        if username:
            acc = supabase_client.fetch_account(username) or {}
            supabase_client.push_user_credentials(
                username, api_key=str(acc.get("api_key") or ""),
                site_origin=origin,
                password_hash=str(acc.get("password_hash") or ""),
                salt=str(acc.get("salt") or ""),
                created_at=str(acc.get("created_at") or ""))
    else:
        db.set_setting("site_origin", origin, user_db_path)
        if username:
            db.update_user_settings(username, site_origin=origin, path=db_path)
    return {"ok": True, "site_origin": origin}


def set_proxy(enabled: bool, db_path) -> dict:
    """开关抓取代理（实例级 settings），重启后自动恢复。"""
    config.set_proxy_enabled(enabled)
    db.set_setting("proxy_enabled", "1" if enabled else "0", db_path)
    return {"ok": True, "proxy_enabled": config.PROXY_ENABLED,
            "proxy_address": config.PROXY}
