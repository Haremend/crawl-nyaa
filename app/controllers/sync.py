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
