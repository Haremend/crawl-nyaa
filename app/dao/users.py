"""DAO 用户与会话层：账号、会话、每用户独立库路径、跨库标记。

依赖基础层 ``schema``（连接/锁/通用工具/utc_now）。跨用户库的「标记已下载」
复用作品层 ``galleries.set_downloaded``。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import Any

from .. import config
from .schema import _WRITE_LOCK, connect, utc_now

# 保留 db._WRITE_LOCK 的引用（server._cloud_login_fallback 直接摸它做本地重建）
__all__ = ["_WRITE_LOCK"]


def user_db_path(username: str, base_dir=None):
    """每个登录用户一份独立 SQLite：data/users/<username>.sqlite3。

    这样现有所有 db.* 函数（都以 path 参数运行）可以原样复用，
    不同用户的爬取数据天然隔离；users/sessions 表则始终在默认库里。
    """
    name = (username or "").strip()
    if not name:
        return Path(config.DB_PATH)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name).strip("._")
    safe = safe or "user"
    if base_dir is not None:
        # 兼容旧调用：base_dir 是「数据根目录」，用户库放在它下面的 users/
        root = Path(base_dir) / "users"
    else:
        # 以 config.USER_DATA_DIR 为准（可用 CLAW_USER_DATA_DIR 覆盖）；
        # 默认值就是 DATA_DIR/users，与历史布局一致。
        root = Path(config.USER_DATA_DIR)
    return root / f"{safe}.sqlite3"


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """PBKDF2-SHA256 哈希；返回 (salt, hex_digest)。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 120_000
    ).hex()
    return salt, digest


def register_user(username: str, password: str, *, api_key: str = "",
                  site_origin: str = "", path: str | Path | None = None) -> str | None:
    """创建用户；用户名已存在返回 None；参数不合法抛 ValueError。"""
    name = (username or "").strip()
    if not (3 <= len(name) <= 64):
        raise ValueError("用户名长度需在 3-64 个字符之间")
    if not password or len(password) < 4:
        raise ValueError("密码至少 4 位")
    salt, digest = hash_password(password)
    now = utc_now()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt, api_key, site_origin, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, digest, salt, (api_key or "").strip(), (site_origin or "").strip(), now),
                )
                conn.commit()
                return name
            except sqlite3.IntegrityError:
                return None
        finally:
            conn.close()


def verify_user(username: str, password: str, path: str | Path | None = None) -> bool:
    name = (username or "").strip()
    if not name:
        return False
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT salt, password_hash FROM users WHERE username = ?", (name,)
        ).fetchone()
        if not row:
            return False
        _, digest = hash_password(password, str(row["salt"]))
        return secrets.compare_digest(digest, str(row["password_hash"]))
    finally:
        conn.close()


def create_session(username: str, path: str | Path | None = None,
                   ttl_hours: int | None = None) -> str:
    """为用户创建会话 token；同一用户旧会话被清理。"""
    ttl = int(ttl_hours if ttl_hours is not None else config.SESSION_TTL_HOURS)
    token = secrets.token_urlsafe(32)
    now = utc_now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl)).isoformat(timespec="seconds")
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute("DELETE FROM sessions WHERE username = ? OR expires_at <= ?", (username, now))
            conn.execute(
                "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, username, now, expires),
            )
            conn.commit()
            return token
        finally:
            conn.close()


def get_user_by_session(token: str, path: str | Path | None = None) -> dict | None:
    """按会话 token 取用户安全视图（不含密码字段）；过期/不存在返回 None。"""
    token = (token or "").strip()
    if not token:
        return None
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT s.username, u.api_key, u.site_origin, u.created_at "
            "FROM sessions s JOIN users u ON u.username = s.username "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, utc_now()),
        ).fetchone()
        if not row:
            return None
        return {"username": str(row["username"]), "api_key": str(row["api_key"] or ""),
                "site_origin": str(row["site_origin"] or ""), "created_at": str(row["created_at"] or "")}
    finally:
        conn.close()


def get_user(username: str, path: str | Path | None = None) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT username, api_key, site_origin, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def update_user_settings(username: str, *, api_key: str | None = None,
                         site_origin: str | None = None,
                         path: str | Path | None = None) -> None:
    updates: list[str] = []
    params: list[Any] = []
    if api_key is not None:
        updates.append("api_key = ?")
        params.append(str(api_key).strip())
    if site_origin is not None:
        updates.append("site_origin = ?")
        params.append(str(site_origin).strip())
    if not updates:
        return
    params.append(username)
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE username = ?", params)
            conn.commit()
        finally:
            conn.close()


def delete_session(token: str, path: str | Path | None = None) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ============================================================
# 多用户辅助：用户库列表与跨库标记
# ============================================================

def list_usernames(path: str | Path | None = None) -> list[str]:
    """默认库里注册的全部用户名（用于跨用户库操作，例如标记已下载）。"""
    conn = connect(path)
    try:
        return [str(row["username"]) for row in conn.execute(
            "SELECT username FROM users ORDER BY username").fetchall()]
    finally:
        conn.close()


def iter_user_db_paths(path: str | Path | None = None) -> list:
    """所有用户独立库的路径（不保证存在，调用方需自行判断）。"""
    return [user_db_path(name) for name in list_usernames(path)]


def mark_downloaded_all(gallery_id: int, path: str | Path | None = None) -> int:
    """把某个作品在所有库（默认库 + 每个用户库）里标记为已下载。

    文件在服务器上只存一份，因此「已下载」是全局事实：任何用户触发下载后，
    其他持有同一作品的用户也应该看到「已下载」。
    """
    from .galleries import set_downloaded
    from .schema import _optional_int

    gid = _optional_int(gallery_id)
    if not gid:
        return 0
    targets = [path or config.DB_PATH] + iter_user_db_paths(path)
    changed = 0
    for target in targets:
        try:
            if not Path(target).exists():
                continue
            changed += set_downloaded([gid], True, target)
        except sqlite3.Error:
            continue
    return changed
