"""DAO 设置层：实例级 key/value 设置与站点地址推导。

依赖基础层 ``schema``。``get_site_origin`` 会直接读 ``scrape_runs`` 与
``galleries`` 表（跨域只读），用内联 SQL 即可，无需引入 runs 域避免循环。
"""
from __future__ import annotations

from pathlib import Path

from .. import config
from .schema import _WRITE_LOCK, connect


def get_setting(key: str, default: str = "", path: str | Path | None = None) -> str:
    """读取全局设置（不存在时返回默认值）。"""
    conn = connect(path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
        return str(row[0]) if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str, path: str | Path | None = None) -> None:
    """写入全局设置。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            conn.commit()
        finally:
            conn.close()


def get_site_origin(path: str | Path | None = None) -> str:
    """上游站点地址的取值顺序：

    1. 前端在门禁里填写的域名（settings.site_origin，最优先）
    2. 环境变量 CLAW_API_ORIGIN
    3. 最近一次抓取的列表页 URL 推导
    4. 库内任一作品链接推导
    """
    saved = get_setting("site_origin", "", path).strip()
    if saved:
        return saved.rstrip("/")
    if config.API_ORIGIN:
        return config.API_ORIGIN.rstrip("/")
    candidates: list[str] = []
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT url FROM scrape_runs WHERE url LIKE 'http%' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row:
            candidates.append(str(row[0]))
        link = conn.execute(
            "SELECT url FROM galleries WHERE url LIKE 'http%' LIMIT 1"
        ).fetchone()
        if link:
            candidates.append(str(link[0]))
    finally:
        conn.close()
    from urllib.parse import urlsplit
    for value in candidates:
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""
