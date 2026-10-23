"""controller：同步域（sync/status + VPS 远程端点）。门禁留在 Handler。"""
from __future__ import annotations

from app.services import sync_service


def get_sync_status(h) -> None:
    h._json(sync_service.status())


def post_sync(h) -> None:
    user = h._ensure_login()
    if not user:
        return
    _respond(h, sync_service.sync(user), "同步失败")


def post_sync_pull(h) -> None:
    user = h._ensure_login()
    if not user:
        return
    _respond(h, sync_service.pull(user), "拉取失败")


def post_sync_push(h) -> None:
    user = h._ensure_login()
    if not user:
        return
    _respond(h, sync_service.push(user), "推送失败")


def post_sync_cross(h) -> None:
    """跨账号同步（需求基线 §12.3）：把 unlogin_user（或源账号）的数据合并进目标账号。

    未登录：允许调用，数据源固定为 unlogin_user 库；target_user 缺省 = unlogin_user
    （即常规同步），传已登录账号名则合并到该账号。
    已登录：数据源为登录账号库；target_user 缺省 = 当前登录账号。
    请求体 {target_user, source_user}，source_user 可选（第三方账号的云端数据一并并入）。
    """
    user = h._current_user()   # 允许未登录（匿名 → unlogin_user）
    payload = h._read_json() or {}
    result = sync_service.cross_sync(
        user,
        target_user=str(payload.get("target_user") or ""),
        source_user=str(payload.get("source_user") or ""))
    _respond(h, result, "跨账号同步失败")


def _respond(h, result: dict, default_error: str) -> None:
    if not result.get("ok") and result.get("status"):
        h._error(result.get("error", default_error), result["status"])
        return
    h._json(result)


# ---------- VPS 远程端点（Handler 先过 _require_remote_guard 再调这里）----------

def get_remote_account(h, query) -> None:
    if not h._require_remote_guard():
        return
    values = (query or {}).get("username") or []
    username = str(values[0] if values else "").strip()
    result = sync_service.remote_account_get(username)
    if not result.get("ok"):
        h._error(result.get("error", "缺少 username"), result.get("status", 400))
        return
    h._json(result)


def post_remote_account(h) -> None:
    if not h._require_remote_guard():
        return
    payload = h._read_json()
    if payload is None:
        return
    result = sync_service.remote_account_post(payload)
    if not result.get("ok"):
        h._error(result.get("error", "缺少 username"), result.get("status", 400))
        return
    h._json(result)


def post_remote_sync(h) -> None:
    if not h._require_remote_guard():
        return
    payload = h._read_json()
    if payload is None:
        return
    result = sync_service.remote_sync(payload)
    if not result.get("ok"):
        h._error(result.get("error", "缺少 username"), result.get("status", 400))
        return
    h._json(result)
