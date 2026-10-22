"""auth 业务逻辑：注册（云优先）+ 登录（本地优先 + 云回退）。

从 app/server.py 的 _handle_register / _handle_login / _cloud_login_fallback
抽出。每个函数返回结构化 dict，controller 负责 HTTP 格式化（_json/_error）。
行为与拆分前完全一致。
"""
from __future__ import annotations

import secrets

from app import config, db, remote_client, supabase_client
from app.services._log import cloud_log

# normalize_origin 在 app/server.py 是模块级函数；直接 import 会因 server.py
# 又 import 本模块造成循环。这里本地实现一份同逻辑的副本（纯字符串规范化，
# 无业务副作用）。
from urllib.parse import urlsplit


def normalize_origin(raw: str) -> str:
    """把用户填写的域名规范成 https://host[:port]；无效时返回空字符串。"""
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").strip().lower()
    if not host:
        return ""
    if "." not in host and host != "localhost":
        return ""
    scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    return f"{scheme}://{host}{port}"


def register(username: str, password: str, api_key: str, site_origin: str,
            db_path) -> dict:
    """注册：云优先（remote VPS → Supabase）→ 本地落库 + 建会话。

    返回 {ok, username, cloud_used, cloud_error, session_token, max_age,
    error, status}。controller 据此 _json/_error。
    """
    username = str(username or "").strip()
    password = str(password or "")
    api_key = str(api_key or "").strip()
    site_origin = normalize_origin(str(site_origin or ""))
    cloud_used = False
    cloud_error = ""
    salt, digest = db.hash_password(password, salt=None)
    if config.remote_configured():
        try:
            cloud_log(f"注册→VPS upsert 账号 user={username}")
            remote_client.get_client().upsert_account(
                username, api_key=api_key, site_origin=site_origin,
                password_hash=digest, salt=salt, created_at=db.utc_now())
            cloud_used = True
        except remote_client.RemoteError as exc:
            cloud_log(f"注册→VPS 失败 user={username}: {exc} (HTTP {exc.status})")
            return {"ok": False, "error": f"注册失败：无法写入远程 VPS：{exc}",
                    "status": 400}
    elif supabase_client.configured():
        try:
            cloud_log(f"注册→Supabase 直写凭据 user={username}")
            supabase_client.ensure_tables()
            supabase_client.push_user_credentials(
                username, api_key=api_key, site_origin=site_origin,
                password_hash=digest, salt=salt, created_at=db.utc_now())
            cloud_used = True
        except (supabase_client.SupabaseError, ValueError) as exc:
            cloud_log(f"注册→Supabase 失败 user={username}: {exc}")
            cloud_error = str(exc)
    try:
        created = db.register_user(username, password, api_key=api_key,
                                   site_origin=site_origin, path=db_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}
    if not created:
        return {"ok": False, "error": "用户名已被占用", "status": 409}
    if not config.storage_is_cloud():
        db.init_db(db.user_db_path(created))
    token = db.create_session(created, path=db_path)
    max_age = config.SESSION_TTL_HOURS * 3600
    return {"ok": True, "username": created, "cloud_used": cloud_used,
            "cloud_error": cloud_error, "session_token": token,
            "max_age": max_age}


def login(username: str, password: str, db_path) -> dict:
    """登录：本地 verify 优先；失败且（remote 或 supabase 可用）时走云回退。

    返回 {ok, username, session_token, max_age, error, status}。
    """
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return {"ok": False, "error": "请输入用户名和密码", "status": 400}
    local_ok = db.verify_user(username, password, db_path)
    if not local_ok:
        if not ((config.remote_configured() or supabase_client.configured())
                and _cloud_login_fallback(username, password, db_path)):
            return {"ok": False, "error": "用户名或密码错误", "status": 401}
    token = db.create_session(username, path=db_path)
    max_age = config.SESSION_TTL_HOURS * 3600
    return {"ok": True, "username": username, "session_token": token,
            "max_age": max_age}


def _cloud_login_fallback(username: str, password: str, db_path) -> bool:
    """本地无该用户时，从云端账号表拉取记录并在本地重建后验证。

    拉账号那一步按配置分流：配了 CLAW_REMOTE_BASE 走 VPS；否则直连 Supabase。
    验证通过后走 db.upsert_user_credentials（不再直接摸 db._WRITE_LOCK）。
    """
    if config.remote_configured():
        try:
            cloud_log(f"登录云回退→VPS 读账号 user={username}")
            account = remote_client.get_client().fetch_account(username)
        except remote_client.RemoteError as exc:
            cloud_log(f"登录云回退→VPS 读账号失败 user={username}: {exc} "
                      f"(HTTP {exc.status})")
            return False
    else:
        try:
            cloud_log(f"登录云回退→Supabase 读账号 user={username}")
            account = supabase_client.fetch_account(username)
        except supabase_client.SupabaseError as exc:
            cloud_log(f"登录云回退→Supabase 读账号失败 user={username}: {exc}")
            return False
    if not account:
        return False
    salt = str(account.get("salt") or "")
    stored_hash = str(account.get("password_hash") or "")
    if not stored_hash or not salt:
        return False
    _, digest = db.hash_password(password, salt)
    if not secrets.compare_digest(digest, stored_hash):
        return False
    # 验证通过：本地账号库重建该用户记录（先建行满足唯一约束，再覆写真实哈希）
    if not db.get_user(username, db_path):
        db.register_user(username, "__cloud__placeholder__", path=db_path)
    db.upsert_user_credentials(
        username, password_hash=stored_hash, salt=salt,
        api_key=str(account.get("api_key") or ""),
        site_origin=str(account.get("site_origin") or ""), path=db_path)
    if not config.storage_is_cloud():
        db.init_db(db.user_db_path(username))
    return True
