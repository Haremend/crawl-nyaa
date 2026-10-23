"""controller：认证域（注册/登录/退出/me）。只读请求 + 调 service + 格式化。"""
from __future__ import annotations

from app import db
from app.services import auth_service

# SESSION_COOKIE 与 _parse_cookies 是 app.server 的模块级常量/函数。这里经
# h（Handler 实例）的类属性拿 SESSION_COOKIE，_parse_cookies 走模块级调用——
# 但 controller 不能 import app.server（循环：server import controllers），
# 所以把 cookie 解析直接内联（与 server 版逐字节一致）。


def _parse_cookies(header):
    """从 Cookie 头解析 dict（与 app.server._parse_cookies 逐字节一致，避免循环 import）。"""
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def get_auth_me(h) -> None:
    user = h._current_user()
    if not user:
        h._json({"ok": True, "logged_in": False, "user": None})
        return
    h._json({
        "ok": True,
        "logged_in": True,
        "user": {
            "username": user.get("username", ""),
            "has_api_key": bool(user.get("api_key")),
            "site_origin": user.get("site_origin", ""),
            "created_at": user.get("created_at", ""),
        },
    })


def post_register(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    result = auth_service.register(
        payload.get("username"), payload.get("password"),
        payload.get("api_key"), payload.get("site_origin"),
        h.server.db_path)
    if not result.get("ok"):
        h._error(result.get("error", "注册失败"), result.get("status", 400))
        return
    h._json({"ok": True, "username": result["username"],
             "cloud_registered": result.get("cloud_used", False),
             "cloud_error": result.get("cloud_error", "")},
             headers={"Set-Cookie": h._session_cookie_header(
                 result["session_token"], max_age=result["max_age"])})


def post_login(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    result = auth_service.login(
        payload.get("username"), payload.get("password"), h.server.db_path,
        sync_creds=bool(payload.get("sync_creds", False)))
    if not result.get("ok"):
        h._error(result.get("error", "用户名或密码错误"), result.get("status", 401))
        return
    h._json({"ok": True, "username": result["username"]},
             headers={"Set-Cookie": h._session_cookie_header(
                 result["session_token"], max_age=result["max_age"])})


def post_logout(h) -> None:
    cookies = _parse_cookies(h.headers.get("Cookie"))
    token = cookies.get("claw_session", "")
    if token:
        db.delete_session(token, path=h.server.db_path)
    h._json({"ok": True},
             headers={"Set-Cookie": h._session_cookie_header("", max_age=0)})
