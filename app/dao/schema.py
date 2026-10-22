"""DAO 基础层：建表/连接/迁移、通用小工具、跨域共享的写锁与列常量。

其他 dao 子模块都从这里取 ``connect`` / ``utc_now`` / ``_WRITE_LOCK`` /
``_chunks`` / ``_optional_int`` 以及同步用的列清单。本模块不依赖任何 dao 业务模块，
只依赖 ``config`` 与标准库，是 dao 层的最底层。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config


# 全局写锁：所有写库操作（入库/标记/批次/任务/设置）串行化，避免 SQLite 写冲突。
# 放在基础层，供 galleries / runs / users / settings / jobs / cloud_merge 共享。
_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS galleries (
    gallery_id        INTEGER PRIMARY KEY,
    media_id          INTEGER,
    title             TEXT NOT NULL DEFAULT '',
    title_pretty      TEXT NOT NULL DEFAULT '',
    title_original    TEXT NOT NULL DEFAULT '',
    title_key         TEXT NOT NULL DEFAULT '',
    original_key      TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL,
    thumbnail         TEXT NOT NULL DEFAULT '',
    languages         TEXT NOT NULL DEFAULT '[]',
    primary_language  TEXT NOT NULL DEFAULT 'unknown',
    tags              TEXT NOT NULL DEFAULT '[]',
    artists           TEXT NOT NULL DEFAULT '[]',
    groups            TEXT NOT NULL DEFAULT '[]',
    parodies          TEXT NOT NULL DEFAULT '[]',
    characters        TEXT NOT NULL DEFAULT '[]',
    categories        TEXT NOT NULL DEFAULT '[]',
    cover_file        TEXT NOT NULL DEFAULT '',
    cover_fetched_at  TEXT NOT NULL DEFAULT '',
    scanlator         TEXT NOT NULL DEFAULT '',
    uncensored        INTEGER NOT NULL DEFAULT 0,
    want_download     INTEGER NOT NULL DEFAULT 0,
    downloaded        INTEGER NOT NULL DEFAULT 0,
    downloaded_at     TEXT NOT NULL DEFAULT '',
    hidden            INTEGER NOT NULL DEFAULT 0,
    site_favorite     INTEGER NOT NULL DEFAULT 0,
    from_scrape       INTEGER NOT NULL DEFAULT 1,
    batch_id          INTEGER NOT NULL DEFAULT 0,
    num_pages         INTEGER,
    num_favorites     INTEGER,
    upload_date       TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL DEFAULT '',
    detail_fetched_at TEXT NOT NULL DEFAULT '',
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_galleries_original_key ON galleries(original_key);
CREATE INDEX IF NOT EXISTS idx_galleries_title_key ON galleries(title_key);
CREATE INDEX IF NOT EXISTS idx_galleries_language ON galleries(primary_language);
CREATE INDEX IF NOT EXISTS idx_galleries_last_seen ON galleries(last_seen);

CREATE TABLE IF NOT EXISTS download_jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT '',
    gallery_id   INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    format       TEXT NOT NULL DEFAULT 'zip',
    source       TEXT NOT NULL DEFAULT 'manual',
    batch_id     INTEGER,
    status       TEXT NOT NULL DEFAULT 'queued',
    total_bytes  INTEGER NOT NULL DEFAULT 0,
    done_bytes   INTEGER NOT NULL DEFAULT 0,
    file_path    TEXT NOT NULL DEFAULT '',
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_gallery ON download_jobs(gallery_id);
-- 注意：idx_jobs_username 不放在 SCHEMA 里，否则旧库（download_jobs 还没有
-- username 列）会在 executescript 阶段建索引失败；它在下面的迁移块里建。

CREATE TABLE IF NOT EXISTS scrape_jobs (
    job_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    options     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_jobs_status ON scrape_jobs(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    api_key       TEXT NOT NULL DEFAULT '',
    site_origin   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    pages_total INTEGER NOT NULL DEFAULT 0,
    pages_done  INTEGER NOT NULL DEFAULT 0,
    items_seen  INTEGER NOT NULL DEFAULT 0,
    items_new   INTEGER NOT NULL DEFAULT 0,
    items_dup   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""

# 旧数据库升级：缺失列名 -> ALTER 语句
MIGRATIONS = {
    "title_pretty": "ALTER TABLE galleries ADD COLUMN title_pretty TEXT NOT NULL DEFAULT ''",
    "title_original": "ALTER TABLE galleries ADD COLUMN title_original TEXT NOT NULL DEFAULT ''",
    "original_key": "ALTER TABLE galleries ADD COLUMN original_key TEXT NOT NULL DEFAULT ''",
    "scanlator": "ALTER TABLE galleries ADD COLUMN scanlator TEXT NOT NULL DEFAULT ''",
    "cover_file": "ALTER TABLE galleries ADD COLUMN cover_file TEXT NOT NULL DEFAULT ''",
    "cover_fetched_at": "ALTER TABLE galleries ADD COLUMN cover_fetched_at TEXT NOT NULL DEFAULT ''",
    "uncensored": "ALTER TABLE galleries ADD COLUMN uncensored INTEGER NOT NULL DEFAULT 0",
    "want_download": "ALTER TABLE galleries ADD COLUMN want_download INTEGER NOT NULL DEFAULT 0",
    "downloaded": "ALTER TABLE galleries ADD COLUMN downloaded INTEGER NOT NULL DEFAULT 0",
    "downloaded_at": "ALTER TABLE galleries ADD COLUMN downloaded_at TEXT NOT NULL DEFAULT ''",
    "hidden": "ALTER TABLE galleries ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
    "site_favorite": "ALTER TABLE galleries ADD COLUMN site_favorite INTEGER NOT NULL DEFAULT 0",
    "from_scrape": "ALTER TABLE galleries ADD COLUMN from_scrape INTEGER NOT NULL DEFAULT 1",
    "batch_id": "ALTER TABLE galleries ADD COLUMN batch_id INTEGER NOT NULL DEFAULT 0",
    "num_pages": "ALTER TABLE galleries ADD COLUMN num_pages INTEGER",
    "num_favorites": "ALTER TABLE galleries ADD COLUMN num_favorites INTEGER",
    "upload_date": "ALTER TABLE galleries ADD COLUMN upload_date TEXT NOT NULL DEFAULT ''",
    "detail_fetched_at": "ALTER TABLE galleries ADD COLUMN detail_fetched_at TEXT NOT NULL DEFAULT ''",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开数据库连接（每次调用独立连接，配合 WAL 支持多线程）。"""
    db_path = Path(path) if path else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.OperationalError as exc:
        # 常见原因：父目录建不了（权限/磁盘只读/路径不对）、文件系统只读。
        # 把路径与原始错误一起抛，避免「unable to open database file」无头绪。
        raise sqlite3.OperationalError(
            f"无法打开数据库 {db_path}（父目录 {db_path.parent}，"
            f"请确认该目录可写、磁盘未只读、路径存在）：{exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: str | Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(galleries)")}
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                conn.execute(statement)
        # 老版本用 favorite 列记录本地标记，现改名为 want_download（保留数据）
        columns_now = {row[1] for row in conn.execute("PRAGMA table_info(galleries)")}
        if "favorite" in columns_now and "want_download" in columns_now:
            conn.execute("UPDATE galleries SET want_download = favorite WHERE favorite = 1")
            try:
                conn.execute("ALTER TABLE galleries DROP COLUMN favorite")
            except Exception:
                pass  # 旧版 SQLite 不支持 DROP COLUMN 时保留该列，不影响使用
        # download_jobs 多用户改造：老队列表补 username / origin 列
        job_cols = {row[1] for row in conn.execute("PRAGMA table_info(download_jobs)")}
        if "username" not in job_cols:
            conn.execute("ALTER TABLE download_jobs ADD COLUMN username TEXT NOT NULL DEFAULT ''")
        if "origin" not in job_cols:
            conn.execute("ALTER TABLE download_jobs ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
        # 列补齐后再建索引（新库列已存在，旧库刚 ALTER 过），两条路径都不会失败
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_username ON download_jobs(username)")
        conn.commit()
    finally:
        conn.close()


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


# ============================================================
# 同步用的列清单（galleries / scrape_runs）
# ============================================================

_GALLERY_COLUMNS = [
    "gallery_id", "media_id", "title", "title_pretty", "title_original", "title_key",
    "original_key", "url", "thumbnail", "languages", "primary_language", "tags", "artists",
    "groups", "parodies", "characters", "categories", "cover_file", "cover_fetched_at",
    "scanlator", "uncensored", "want_download", "downloaded", "downloaded_at", "hidden",
    "site_favorite", "from_scrape", "batch_id", "num_pages", "num_favorites", "upload_date",
    "source_url", "detail_fetched_at", "first_seen", "last_seen",
]

# 同步时不参与「新者胜」判断的本地状态字段（云端数据不覆盖这些本地标记）
_GALLERY_INT_COLUMNS = (
    "media_id", "uncensored", "want_download", "downloaded", "hidden",
    "site_favorite", "from_scrape", "batch_id", "num_pages", "num_favorites",
)

# 用户操作标记：同步合并时「1 覆盖 0」（done 覆盖 todo，逻辑删除覆盖未删除）。
# 即 max(本地, 云端) —— 任一方为 1 则结果为 1，不受 last_seen 影响。
_GALLERY_MARK_COLUMNS = ("want_download", "downloaded", "hidden", "site_favorite")

# 内容字段（非标记、非主键、非时间戳）：较新者胜（excluded.last_seen >= 本地时才覆盖）
_GALLERY_CONTENT_COLUMNS = tuple(
    c for c in _GALLERY_COLUMNS
    if c != "gallery_id" and c not in _GALLERY_MARK_COLUMNS and c not in ("last_seen", "first_seen")
)

_RUN_COLUMNS = ["url", "status", "started_at", "finished_at", "pages_total", "pages_done",
                "items_seen", "items_new", "items_dup", "error"]

_RUN_INT_COLUMNS = ("pages_total", "pages_done", "items_seen", "items_new", "items_dup")


def columns_for(table: str) -> list[str]:
    """同步用的列清单（galleries / scrape_runs）。"""
    return _GALLERY_COLUMNS if table == "galleries" else _RUN_COLUMNS


def int_columns_for(table: str) -> tuple:
    return _GALLERY_INT_COLUMNS if table == "galleries" else _RUN_INT_COLUMNS
