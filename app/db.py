"""SQLite 存取层：作品条目入库、详情补全、去重聚类与查询。

去重分三层：
1. gallery_id 主键：同一 ID 永远只保留一行（幂等 upsert）；
2. original_key：详情 API 的原始标题（日文）归一化后的精确键 —— 同一作品的不同语言版本共用；
3. 标题指纹模糊合并：对还没有详情数据的条目做保守合并（卷号签名 + 前缀/相似度 + 括号守卫）。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config, titles

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

UPSERT_SQL = """
INSERT INTO galleries (
    gallery_id, media_id, title, title_pretty, title_original, title_key, original_key,
    url, thumbnail, languages, primary_language, tags, artists, groups, parodies,
    characters, categories, scanlator, cover_file, cover_fetched_at, uncensored, want_download, downloaded, downloaded_at,
    site_favorite, from_scrape, batch_id, num_pages, num_favorites, upload_date,
    source_url, detail_fetched_at, first_seen, last_seen
) VALUES (
    :gallery_id, :media_id, :title, :title_pretty, :title_original, :title_key, :original_key,
    :url, :thumbnail, :languages, :primary_language, :tags, :artists, :groups, :parodies,
    :characters, :categories, :scanlator, :cover_file, :cover_fetched_at, :uncensored, :want_download, :downloaded, :downloaded_at,
    :site_favorite, :from_scrape, :batch_id, :num_pages, :num_favorites, :upload_date,
    :source_url, :detail_fetched_at, :first_seen, :last_seen
)
ON CONFLICT(gallery_id) DO UPDATE SET
    media_id          = COALESCE(excluded.media_id, galleries.media_id),
    title             = CASE WHEN excluded.title <> '' THEN excluded.title ELSE galleries.title END,
    title_pretty      = CASE WHEN excluded.title_pretty <> '' THEN excluded.title_pretty ELSE galleries.title_pretty END,
    title_original    = CASE WHEN excluded.title_original <> '' THEN excluded.title_original ELSE galleries.title_original END,
    title_key         = CASE WHEN excluded.title_key <> '' THEN excluded.title_key ELSE galleries.title_key END,
    original_key      = CASE WHEN excluded.original_key <> '' THEN excluded.original_key ELSE galleries.original_key END,
    url               = CASE WHEN excluded.url <> '' THEN excluded.url ELSE galleries.url END,
    thumbnail         = CASE WHEN excluded.thumbnail <> '' THEN excluded.thumbnail ELSE galleries.thumbnail END,
    languages         = CASE WHEN excluded.languages NOT IN ('[]', '') THEN excluded.languages ELSE galleries.languages END,
    primary_language  = CASE WHEN excluded.languages NOT IN ('[]', '') THEN excluded.primary_language ELSE galleries.primary_language END,
    tags              = CASE WHEN excluded.tags NOT IN ('[]', '') THEN excluded.tags ELSE galleries.tags END,
    artists           = CASE WHEN excluded.artists NOT IN ('[]', '') THEN excluded.artists ELSE galleries.artists END,
    groups            = CASE WHEN excluded.groups NOT IN ('[]', '') THEN excluded.groups ELSE galleries.groups END,
    parodies          = CASE WHEN excluded.parodies NOT IN ('[]', '') THEN excluded.parodies ELSE galleries.parodies END,
    characters        = CASE WHEN excluded.characters NOT IN ('[]', '') THEN excluded.characters ELSE galleries.characters END,
    categories        = CASE WHEN excluded.categories NOT IN ('[]', '') THEN excluded.categories ELSE galleries.categories END,
    scanlator         = CASE WHEN excluded.scanlator <> '' THEN excluded.scanlator ELSE galleries.scanlator END,
    uncensored        = CASE WHEN excluded.uncensored = 1 THEN 1 ELSE galleries.uncensored END,
    want_download     = CASE WHEN excluded.want_download = 1 THEN 1 ELSE galleries.want_download END,
    downloaded        = CASE WHEN excluded.downloaded = 1 THEN 1 ELSE galleries.downloaded END,
    downloaded_at     = CASE WHEN excluded.downloaded_at <> '' THEN excluded.downloaded_at ELSE galleries.downloaded_at END,
    site_favorite     = CASE WHEN excluded.site_favorite = 1 THEN 1 ELSE galleries.site_favorite END,
    from_scrape       = CASE WHEN excluded.from_scrape = 1 THEN 1 ELSE galleries.from_scrape END,
    batch_id          = CASE WHEN excluded.batch_id > 0 THEN excluded.batch_id ELSE galleries.batch_id END,
    cover_file        = CASE WHEN excluded.cover_file <> '' THEN excluded.cover_file ELSE galleries.cover_file END,
    cover_fetched_at  = CASE WHEN excluded.cover_fetched_at <> '' THEN excluded.cover_fetched_at ELSE galleries.cover_fetched_at END,
    num_pages         = COALESCE(excluded.num_pages, galleries.num_pages),
    num_favorites     = COALESCE(excluded.num_favorites, galleries.num_favorites),
    upload_date       = CASE WHEN excluded.upload_date <> '' THEN excluded.upload_date ELSE galleries.upload_date END,
    source_url        = CASE WHEN excluded.source_url <> '' THEN excluded.source_url ELSE galleries.source_url END,
    detail_fetched_at = CASE WHEN excluded.detail_fetched_at <> '' THEN excluded.detail_fetched_at ELSE galleries.detail_fetched_at END,
    last_seen         = excluded.last_seen
"""

APPLY_DETAIL_SQL = """
UPDATE galleries SET
    media_id          = COALESCE(:media_id, media_id),
    title_pretty      = CASE WHEN :title_pretty <> '' THEN :title_pretty ELSE title_pretty END,
    title_original    = CASE WHEN :title_original <> '' THEN :title_original ELSE title_original END,
    original_key      = CASE WHEN :original_key <> '' THEN :original_key ELSE original_key END,
    thumbnail         = CASE WHEN :thumbnail <> '' THEN :thumbnail ELSE thumbnail END,
    languages         = CASE WHEN :languages NOT IN ('[]', '') THEN :languages ELSE languages END,
    primary_language  = CASE WHEN :languages NOT IN ('[]', '') THEN :primary_language ELSE primary_language END,
    tags              = CASE WHEN :tags NOT IN ('[]', '') THEN :tags ELSE tags END,
    artists           = CASE WHEN :artists NOT IN ('[]', '') THEN :artists ELSE artists END,
    groups            = CASE WHEN :groups NOT IN ('[]', '') THEN :groups ELSE groups END,
    parodies          = CASE WHEN :parodies NOT IN ('[]', '') THEN :parodies ELSE parodies END,
    characters        = CASE WHEN :characters NOT IN ('[]', '') THEN :characters ELSE characters END,
    categories        = CASE WHEN :categories NOT IN ('[]', '') THEN :categories ELSE categories END,
    scanlator         = CASE WHEN :scanlator <> '' THEN :scanlator ELSE scanlator END,
    uncensored        = CASE WHEN :uncensored = 1 THEN 1 ELSE uncensored END,
    num_pages         = COALESCE(:num_pages, num_pages),
    num_favorites     = COALESCE(:num_favorites, num_favorites),
    upload_date       = CASE WHEN :upload_date <> '' THEN :upload_date ELSE upload_date END,
    detail_fetched_at = :detail_fetched_at
WHERE gallery_id = :gallery_id
"""


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


def _item_to_row(item: Any) -> dict:
    if hasattr(item, "as_row"):
        return dict(item.as_row())
    return dict(item)


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _json_list(value: Any) -> str:
    return json.dumps([str(entry) for entry in _as_list(value) if str(entry).strip()], ensure_ascii=False)


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _params_from_row(gid: int, row: dict, source_url: str, now: str) -> dict:
    languages = [str(entry).lower() for entry in _as_list(row.get("languages")) if str(entry).strip()]
    return {
        "gallery_id": gid,
        "media_id": _optional_int(row.get("media_id")),
        "title": str(row.get("title") or f"#{gid}"),
        "title_pretty": str(row.get("title_pretty") or ""),
        "title_original": str(row.get("title_original") or ""),
        "title_key": str(row.get("title_key") or f"id:{gid}"),
        "original_key": str(row.get("original_key") or ""),
        "url": str(row.get("url") or f"/g/{gid}/"),
        "thumbnail": str(row.get("thumbnail") or ""),
        "languages": json.dumps(languages, ensure_ascii=False),
        "primary_language": languages[0] if languages else "unknown",
        "tags": _json_list(row.get("tags")),
        "artists": _json_list(row.get("artists")),
        "groups": _json_list(row.get("groups")),
        "parodies": _json_list(row.get("parodies")),
        "characters": _json_list(row.get("characters")),
        "categories": _json_list(row.get("categories")),
        "scanlator": str(row.get("scanlator") or ""),
        "uncensored": 1 if (row.get("uncensored") or 0) else 0,
        "want_download": 1 if (row.get("want_download") or 0) else 0,
        "downloaded": 1 if (row.get("downloaded") or 0) else 0,
        "downloaded_at": str(row.get("downloaded_at") or ""),
        "hidden": 1 if (row.get("hidden") or 0) else 0,
        "site_favorite": 1 if (row.get("site_favorite") or 0) else 0,
        "from_scrape": 1 if (row.get("from_scrape", 1) or 0) else 0,
        "batch_id": _optional_int(row.get("batch_id")) or 0,
        "cover_file": str(row.get("cover_file") or ""),
        "cover_fetched_at": str(row.get("cover_fetched_at") or ""),
        "num_pages": _optional_int(row.get("num_pages")),
        "num_favorites": _optional_int(row.get("num_favorites")),
        "upload_date": str(row.get("upload_date") or ""),
        "source_url": source_url or "",
        "detail_fetched_at": str(row.get("detail_fetched_at") or ""),
        "first_seen": now,
        "last_seen": now,
    }


_CONTENT_FIELDS = (
    "media_id", "title", "title_pretty", "title_original", "title_key", "original_key",
    "url", "thumbnail", "primary_language", "scanlator", "num_pages", "num_favorites",
    "upload_date", "uncensored",
)
_CONTENT_JSON_FIELDS = (
    "languages", "tags", "artists", "groups", "parodies", "characters", "categories",
)


def _same_json(old_value, new_value) -> bool:
    """比较 JSON 字段（顺序无关）。"""
    try:
        old_list = json.loads(old_value or "[]")
    except (ValueError, TypeError):
        old_list = []
    try:
        new_list = json.loads(new_value or "[]")
    except (ValueError, TypeError):
        new_list = []
    return sorted(map(str, old_list)) == sorted(map(str, new_list))


def content_differs(existing, params: dict) -> bool:
    """判断「本次抓到的数据」是否与库中已有记录不同。

    只比较本次真正提供的字段（空值 / 空数组视为未提供），因此
    列表页抓取不会因为「缺少详情数据」而被判为数据变化。
    """
    for field in _CONTENT_FIELDS:
        new = params.get(field)
        if field in ("num_pages", "num_favorites"):
            if new is None:
                continue
            if int(existing[field] or 0) != int(new):
                return True
            continue
        if field == "uncensored":
            if not new:
                continue
            if not (existing[field] or 0):
                return True
            continue
        new_text = "" if new is None else str(new)
        if not new_text.strip():
            continue
        if str(existing[field] or "") != new_text:
            return True
    for field in _CONTENT_JSON_FIELDS:
        new_text = params.get(field) or "[]"
        if new_text in ("[]", ""):
            continue
        if not _same_json(existing[field], new_text):
            return True
    return False


def upsert_items(items: Iterable[Any], *, source_url: str = "", batch_id: int | None = None,
                 path: str | Path | None = None) -> dict:
    """写入作品条目；已存在的记录一律跳过、不覆盖，返回本次新增的 ID 列表。"""
    rows: dict[int, dict] = {}
    for item in items:
        row = _item_to_row(item)
        gid = _optional_int(row.get("gallery_id")) or 0
        if gid <= 0:
            continue
        rows[gid] = row

    if not rows:
        return {"seen": 0, "new": 0, "dup": 0, "new_ids": []}

    now = utc_now()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            existing_rows: dict[int, Any] = {}
            for chunk in _chunks(list(rows.keys()), 400):
                marks = ",".join("?" for _ in chunk)
                for record in conn.execute(
                    f"SELECT * FROM galleries WHERE gallery_id IN ({marks})", chunk
                ):
                    existing_rows[int(record["gallery_id"])] = record
            unchanged = 0   # 已存在且内容一致（同样跳过、不覆盖）
            skipped = 0     # 已存在但本次字段有变化：仍然跳过，不更新库中记录
            new_ids: list[int] = []
            for gid, row in rows.items():
                params = _params_from_row(gid, row, source_url, now)
                if batch_id:                       # 本次抓取的批次号
                    params["batch_id"] = int(batch_id)
                stored = existing_rows.get(gid)
                if stored is not None:
                    # 需求：库中已存在的作品，无论从哪个 URL 再抓到，都直接跳过、
                    # 不更新任何内容字段；保留最早的记录、批次号与来源，
                    # 仅刷新「最近见到」时间（last_seen）作为追踪信息。
                    if content_differs(stored, params):
                        skipped += 1
                    else:
                        unchanged += 1
                    conn.execute("UPDATE galleries SET last_seen = ? WHERE gallery_id = ?",
                                 (now, gid))
                    continue
                conn.execute(UPSERT_SQL, params)
                new_ids.append(gid)
            conn.commit()
        finally:
            conn.close()

    new_count = len(new_ids)
    return {"seen": len(rows), "new": new_count, "dup": len(rows) - new_count,
            "unchanged": unchanged, "skipped": skipped, "updated": 0,
            "new_ids": new_ids}


def ids_needing_cover(ids: Sequence[int], path: str | Path | None = None) -> list[int]:
    """返回还没有本地封面的作品 ID（保持输入顺序）。"""
    wanted: list[int] = []
    for value in ids:
        gid = _optional_int(value) or 0
        if gid > 0 and gid not in wanted:
            wanted.append(gid)
    if not wanted:
        return []

    found: set[int] = set()
    conn = connect(path)
    try:
        for chunk in _chunks(wanted, 400):
            marks = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"SELECT gallery_id FROM galleries WHERE gallery_id IN ({marks}) "
                "AND (cover_file IS NULL OR cover_file = '')",
                chunk,
            )
            found.update(row[0] for row in cursor)
    finally:
        conn.close()
    return [gid for gid in wanted if gid in found]


def cover_urls(ids: Sequence[int], path: str | Path | None = None) -> dict[int, str]:
    """取出这些 ID 的缩略图地址，供下载封面用。"""
    wanted = [gid for gid in (_optional_int(value) or 0 for value in ids) if gid > 0]
    if not wanted:
        return {}
    result: dict[int, str] = {}
    conn = connect(path)
    try:
        for chunk in _chunks(wanted, 400):
            marks = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"SELECT gallery_id, thumbnail FROM galleries WHERE gallery_id IN ({marks})",
                chunk,
            )
            for row in cursor:
                if row[1]:
                    result[int(row[0])] = str(row[1])
    finally:
        conn.close()
    return result


def set_cover(gallery_id: int, filename: str, path: str | Path | None = None) -> bool:
    """记录某作品已下载的本地封面文件名。"""
    gid = _optional_int(gallery_id) or 0
    if gid <= 0 or not filename:
        return False
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE galleries SET cover_file = ?, cover_fetched_at = ? WHERE gallery_id = ?",
                (str(filename), utc_now(), gid),
            )
            conn.commit()
            return bool(cursor.rowcount)
        finally:
            conn.close()


def ids_needing_detail(ids: Sequence[int], path: str | Path | None = None) -> list[int]:
    """返回这些 ID 里还没有补全详情的部分（保持输入顺序）。"""
    wanted: list[int] = []
    for value in ids:
        gid = _optional_int(value) or 0
        if gid > 0 and gid not in wanted:
            wanted.append(gid)
    if not wanted:
        return []

    found: set[int] = set()
    conn = connect(path)
    try:
        for chunk in _chunks(wanted, 400):
            marks = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"SELECT gallery_id FROM galleries WHERE gallery_id IN ({marks}) "
                "AND (detail_fetched_at IS NULL OR detail_fetched_at = '')",
                chunk,
            )
            found.update(row[0] for row in cursor)
    finally:
        conn.close()
    return [gid for gid in wanted if gid in found]


def apply_details(details: Iterable[Any], path: str | Path | None = None) -> int:
    """把详情数据合并到已有条目上（不改动列表页标题，只补全原始标题/标签等）。"""
    rows = [_item_to_row(detail) for detail in details]
    if not rows:
        return 0
    now = utc_now()
    updated = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for row in rows:
                gid = _optional_int(row.get("gallery_id")) or 0
                if gid <= 0:
                    continue
                languages = [str(entry).lower() for entry in _as_list(row.get("languages")) if str(entry).strip()]
                params = {
                    "gallery_id": gid,
                    "media_id": _optional_int(row.get("media_id")),
                    "title_pretty": str(row.get("title_pretty") or ""),
                    "title_original": str(row.get("title_original") or ""),
                    "original_key": str(row.get("original_key") or ""),
                    "thumbnail": str(row.get("thumbnail") or ""),
                    "languages": json.dumps(languages, ensure_ascii=False),
                    "primary_language": languages[0] if languages else "unknown",
                    "tags": _json_list(row.get("tags")),
                    "artists": _json_list(row.get("artists")),
                    "groups": _json_list(row.get("groups")),
                    "parodies": _json_list(row.get("parodies")),
                    "characters": _json_list(row.get("characters")),
                    "categories": _json_list(row.get("categories")),
                    "scanlator": str(row.get("scanlator") or ""),
                    "uncensored": 1 if (row.get("uncensored") or 0) else 0,
                    "num_pages": _optional_int(row.get("num_pages")),
                    "num_favorites": _optional_int(row.get("num_favorites")),
                    "upload_date": str(row.get("upload_date") or ""),
                    "detail_fetched_at": now,
                }
                cursor = conn.execute(APPLY_DETAIL_SQL, params)
                if cursor.rowcount and cursor.rowcount > 0:
                    updated += 1
            conn.commit()
        finally:
            conn.close()
    return updated


def _row_list(row: sqlite3.Row, key: str) -> list:
    try:
        value = json.loads(row[key] or "[]")
    except (ValueError, TypeError, IndexError):
        return []
    return value if isinstance(value, list) else []


def _fetch_rows(q: str | None = None, path: str | Path | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM galleries WHERE hidden = 0"
    params: list = []
    if q:
        like = f"%{q}%"
        sql += (" AND (title LIKE ? OR title_original LIKE ? OR tags LIKE ? OR artists LIKE ? "
                 "OR groups LIKE ? OR parodies LIKE ? OR characters LIKE ?)")
        params = [like] * 7
    sql += " ORDER BY gallery_id DESC"
    conn = connect(path)
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _cluster_rows(rows: Sequence[sqlite3.Row], dedupe_title: bool) -> list[list[sqlite3.Row]]:
    """把条目聚成作品簇：原始标题精确分组 + 标题指纹保守模糊合并。"""
    if not dedupe_title:
        return [[row] for row in rows]

    clusters: list[dict] = []
    index: dict[str, int] = {}
    for row in rows:
        row_keys = row.keys()
        original_key = (row["original_key"] or "").strip() if "original_key" in row_keys else ""
        # 模糊合并统一使用「列表页标题」的指纹：
        # 原始标题（日文）和列表标题（英/中）核心文本完全不同，混用会导致合并失效。
        meta = titles.title_meta(row["title"])
        key = ("orig:" + original_key) if original_key else (meta["core"] or f"id:{row['gallery_id']}")
        position = index.get(key)
        if position is None:
            index[key] = len(clusters)
            clusters.append({"rows": [row], "original_key": original_key, **meta})
        else:
            clusters[position]["rows"].append(row)

    parent = list(range(len(clusters)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left_index: int, right_index: int) -> None:
        left_root, right_root = find(left_index), find(right_index)
        if left_root != right_root:
            parent[right_root] = left_root

    buckets: dict[tuple, list[int]] = {}
    for position, cluster in enumerate(clusters):
        core = cluster["core"]
        for bucket in ((cluster["volumes"], core[:5]), (cluster["volumes"], core[-5:])):
            buckets.setdefault(bucket, []).append(position)

    seen: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 400:
            continue
        for first in range(len(members)):
            for second in range(first + 1, len(members)):
                left_index, right_index = members[first], members[second]
                pair = (left_index, right_index) if left_index < right_index else (right_index, left_index)
                if pair in seen:
                    continue
                seen.add(pair)
                if find(left_index) == find(right_index):
                    continue
                left_cluster, right_cluster = clusters[left_index], clusters[right_index]
                # 两份原始标题都存在且不同 -> 一定是不同作品，禁止模糊合并
                if (left_cluster["original_key"] and right_cluster["original_key"]
                        and left_cluster["original_key"] != right_cluster["original_key"]):
                    continue
                if titles.similar_meta(left_cluster, right_cluster):
                    union(left_index, right_index)

    grouped: dict[int, list[sqlite3.Row]] = {}
    for position, cluster in enumerate(clusters):
        grouped.setdefault(find(position), []).extend(cluster["rows"])
    return list(grouped.values())


def _language_order(langs: Iterable[str], preferred: Sequence[str]) -> list[str]:
    unique: list[str] = []
    for lang in langs:
        if lang and lang not in unique:
            unique.append(lang)
    order: list[str] = []
    for lang in list(preferred) + list(config.DEFAULT_LANGUAGE_ORDER):
        if lang in unique and lang not in order:
            order.append(lang)
    for lang in sorted(unique):
        if lang not in order:
            order.append(lang)
    return order


def _variant_rank(row: sqlite3.Row, preferred: Sequence[str]) -> tuple:
    """选版优先级：无码 > 日语 > 中文 > 英文 > 其他；同级页数多者优先，再新者优先。

    当用户做了语言过滤时，先保证“命中的语言版本”排前面，其余规则不变。
    """
    wanted = set(lang.lower() for lang in preferred if lang and lang.strip())
    lang = row["primary_language"] or "unknown"
    miss_filter = 0 if (not wanted or lang in wanted) else 1
    uncensored = 1 if (row["uncensored"] or 0) else 0
    lang_rank = config.LANGUAGE_PRIORITY.get(lang, len(config.LANGUAGE_PRIORITY))
    pages = int(row["num_pages"] or 0)
    upload = _epoch_from_iso(row["upload_date"] or "")
    return (miss_filter, -uncensored, lang_rank, -pages, -upload, -int(row["gallery_id"]))


def _union(rows: Sequence[sqlite3.Row], key: str) -> list[str]:
    seen: list[str] = []
    for row in rows:
        for value in _row_list(row, key):
            text = str(value).strip()
            if text and text not in seen:
                seen.append(text)
    return seen


def _first_value(rows: Sequence[sqlite3.Row], key: str) -> str:
    for row in rows:
        try:
            value = row[key]
        except (IndexError, KeyError):
            continue
        if value:
            return str(value)
    return ""


def _epoch_from_iso(value: str) -> int:
    """ISO 时间字符串 -> epoch 整数（用于排序）；空值返回 0。"""
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (ValueError, TypeError):
        return 0


def _serialize_group(group: Sequence[sqlite3.Row], preferred: Sequence[str]) -> dict:
    ordered = sorted(group, key=lambda row: _variant_rank(row, preferred))
    main = ordered[0]

    lang_keys: list[str] = []
    for row in ordered:
        for lang in (_row_list(row, "languages") or ["unknown"]):
            lang = str(lang).lower()
            if lang not in lang_keys:
                lang_keys.append(lang)
    if not lang_keys:
        lang_keys = ["unknown"]
    lang_keys = _language_order(lang_keys, preferred)

    thumbnail = ""
    for row in ordered:
        if row["thumbnail"]:
            thumbnail = row["thumbnail"]
            break

    title_pretty = (main["title_pretty"] or "").strip()
    display_title = title_pretty or (main["title"] or "").strip()

    variants = [
        {
            "gallery_id": int(row["gallery_id"]),
            "title": row["title"],
            "title_pretty": row["title_pretty"] or "",
            "title_original": row["title_original"] or "",
            "url": row["url"],
            "language": (row["primary_language"] or "unknown"),
            "language_label": config.language_label(row["primary_language"] or "unknown"),
            "thumbnail": row["thumbnail"],
            "last_seen": row["last_seen"],
            "has_detail": bool(row["detail_fetched_at"]),
            "cover_file": row["cover_file"] or "",
            "uncensored": bool(row["uncensored"]),
            "want_download": bool(row["want_download"]),
            "downloaded": bool(row["downloaded"]),
        }
        for row in ordered
    ]

    pages = max((row["num_pages"] or 0 for row in ordered), default=0)
    favorites = max((row["num_favorites"] or 0 for row in ordered), default=0)
    upload_date = (main["upload_date"] or "").strip()

    return {
        "gallery_id": int(main["gallery_id"]),
        "ids": [int(row["gallery_id"]) for row in ordered],
        "title": display_title,
        "title_raw": main["title"],
        "title_original": _first_value(ordered, "title_original"),
        "url": main["url"],
        "thumbnail": thumbnail,
        "languages": [{"key": key, "label": config.language_label(key)} for key in lang_keys],
        "language_keys": lang_keys,
        "primary_language": (main["primary_language"] or "unknown"),
        "tags": _union(ordered, "tags"),
        "artists": _union(ordered, "artists"),
        "groups": _union(ordered, "groups"),
        "parodies": _union(ordered, "parodies"),
        "characters": _union(ordered, "characters"),
        "categories": _union(ordered, "categories"),
        "scanlator": _first_value(ordered, "scanlator"),
        "num_pages": pages or None,
        "num_favorites": favorites or None,
        "upload_date": upload_date,
        "upload_epoch": _epoch_from_iso(upload_date),
        "has_detail": any(bool(row["detail_fetched_at"]) for row in ordered),
        "uncensored": any(bool(row["uncensored"]) for row in ordered),
        # 「待下载 / 已下载」是作品级的队列标记：组内任一版本被标记，整组就算标记
        # （标记本身仍然只落在推荐版本上，下载时也只下载推荐版本）
        "want_download": any(bool(row["want_download"]) for row in ordered),
        "downloaded": any(bool(row["downloaded"]) for row in ordered),
        "downloaded_at": _first_value(ordered, "downloaded_at"),
        "hidden": any(bool(row["hidden"]) for row in ordered),
        "site_favorite": any(bool(row["site_favorite"]) for row in ordered),
        "batch_id": int(main["batch_id"] or 0),
        "cover_file": _first_value(ordered, "cover_file"),
        "variants": variants,
        "variant_count": len(ordered),
        "first_seen": min(row["first_seen"] for row in ordered),
        "last_seen": max(row["last_seen"] for row in ordered),
    }


def _original_sort_key(item: dict) -> tuple:
    """按作品原名称排序用的 key。

    有原始标题（详情 API 的 title.japanese）的作品先按原名称排序；
    还没补全详情的排在后面，用列表标题占位，保证顺序稳定且可预期。
    """
    original = str(item.get("title_original") or "").strip()
    if original:
        return (0, original.casefold())
    fallback = str(item.get("title_raw") or item.get("title") or "").strip()
    return (1, fallback.casefold())


def _priority_sort_key(item: dict) -> tuple:
    """整表排序键：按主版本的无码/语言/页数/新旧排队，得到“优选清单”。"""
    uncensored = 1 if item.get("uncensored") else 0
    lang = item.get("primary_language") or "unknown"
    lang_rank = config.LANGUAGE_PRIORITY.get(lang, len(config.LANGUAGE_PRIORITY))
    pages = item.get("num_pages") or 0
    upload = _epoch_from_iso(item.get("upload_date") or "")
    return (-uncensored, lang_rank, -upload, -int(item["gallery_id"]))


def _sort_items(items: list[dict], sort: str) -> list[dict]:
    if sort == "priority":
        return sorted(items, key=_priority_sort_key)
    if sort == "original":
        return sorted(items, key=_original_sort_key)
    if sort == "oldest":
        return sorted(items, key=lambda item: item["gallery_id"])
    if sort == "title":
        return sorted(items, key=lambda item: item["title"].casefold())
    if sort == "variants":
        return sorted(items, key=lambda item: (-item["variant_count"], -item["gallery_id"]))
    if sort == "favorites":
        return sorted(items, key=lambda item: (-(item["num_favorites"] or 0), -item["gallery_id"]))
    if sort == "pages":
        return sorted(items, key=lambda item: (-(item["num_pages"] or 0), -item["gallery_id"]))
    return sorted(items, key=lambda item: -item["gallery_id"])  # recent


def query_works(
    *,
    q: str | None = None,
    languages: Sequence[str] | None = None,
    dedupe_title: bool = True,
    sort: str = "recent",
    page: int = 1,
    per_page: int = 24,
    uncensored: bool = False,
    want_download_only: bool = False,
    downloaded_only: bool = False,
    site_favorites_only: bool = False,
    batch_id: int | None = None,
    path: str | Path | None = None,
) -> dict:
    """查询作品列表：支持语言过滤、同作品去重、搜索与排序。"""
    wanted = [str(value).strip().lower() for value in (languages or []) if str(value).strip()]
    rows = _fetch_rows(q, path)
    groups = _cluster_rows(rows, dedupe_title)
    items = [_serialize_group(group, wanted) for group in groups]

    counter: dict[str, int] = {}
    for item in items:
        for key in item["language_keys"]:
            counter[key] = counter.get(key, 0) + 1
    language_stats = [
        {"key": key, "label": config.language_label(key), "count": count}
        for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    if wanted:
        wanted_set = set(wanted)
        items = [item for item in items if wanted_set.intersection(item["language_keys"])]
    if uncensored:
        items = [item for item in items if item["uncensored"]]
    if want_download_only:
        items = [item for item in items if item["want_download"]]
    if downloaded_only:
        items = [item for item in items if item["downloaded"]]
    if site_favorites_only:
        items = [item for item in items if item["site_favorite"]]
    if batch_id:
        items = [item for item in items if item["batch_id"] == int(batch_id)]

    items = _sort_items(items, sort)
    total = len(items)
    per_page = max(1, min(int(per_page or 24), 200))
    pages = max(1, math.ceil(total / per_page)) if total else 1
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page

    return {
        "items": items[start:start + per_page],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "raw_total": len(rows),
        "unique_total": len(groups),
        "dedupe": bool(dedupe_title),
        "sort": sort,
        "language_stats": language_stats,
    }


def set_want_download(gallery_ids: Sequence[int], want: bool, path: str | Path | None = None) -> int:
    """批量设置「待下载」标记（对作品组内所有变体同时生效）。"""
    ids = [_optional_int(gid) or 0 for gid in gallery_ids if _optional_int(gid) or 0]
    if not ids:
        return 0
    value = 1 if want else 0
    updated = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for chunk in _chunks(ids, 400):
                marks = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE galleries SET want_download = ? WHERE gallery_id IN ({marks})",
                    [value] + list(chunk),
                )
                updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
        finally:
            conn.close()
    return updated


def list_batch_targets(batch_id: int, path: str | Path | None = None,
                       *, skip_downloaded: bool = True) -> list[dict]:
    """返回某个批次的全部作品（每部取推荐版本）。

    skip_downloaded=True 时跳过本地已标记「已下载」的作品（重新下载模式传 False）。
    """
    batch = int(batch_id or 0)
    if batch <= 0:
        return []
    conn = connect(path)
    try:
        rows = list(conn.execute("SELECT * FROM galleries WHERE batch_id = ? AND hidden = 0", (batch,)))
    finally:
        conn.close()
    if not rows:
        return []
    targets: list[dict] = []
    for group in _cluster_rows(rows, True):
        # 非重新下载模式：先按数据库的「已下载」标记跳过
        if skip_downloaded and any((row["downloaded"] or 0) for row in group):
            continue
        ordered = sorted(group, key=lambda row: _variant_rank(row, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main["title_pretty"] or main["title"] or ""),
        })
    targets.sort(key=lambda item: -item["gallery_id"])
    return targets


def list_want_download_targets(path: str | Path | None = None,
                               *, skip_downloaded: bool = True) -> list[dict]:
    """返回所有「待下载」作品，每部取该作品的**推荐版本**。

    注意：判断「是否待下载」是按整个作品组来的 —— 只要组内有任意一个版本被标记，
    就下载该组按优先级选出的推荐版本（避免标记后推荐版本变化导致下错版本）。

    skip_downloaded=True 时跳过本地已标记「已下载」的作品（重新下载模式传 False）。
    """
    conn = connect(path)
    try:
        rows = list(conn.execute("SELECT * FROM galleries WHERE hidden = 0"))
    finally:
        conn.close()
    if not rows:
        return []

    targets: list[dict] = []
    for group in _cluster_rows(rows, True):
        if not any((row["want_download"] or 0) for row in group):
            continue
        # 非重新下载模式：先按数据库的「已下载」标记跳过
        if skip_downloaded and any((row["downloaded"] or 0) for row in group):
            continue
        ordered = sorted(group, key=lambda row: _variant_rank(row, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main["title_pretty"] or main["title"] or ""),
        })
    targets.sort(key=lambda item: -item["gallery_id"])
    return targets


def set_downloaded(gallery_ids: Sequence[int], downloaded: bool,
                   path: str | Path | None = None) -> int:
    """批量标记「已下载」（组内所有变体一起标记），并记录时间。"""
    ids = [gid for gid in (_optional_int(value) or 0 for value in gallery_ids) if gid > 0]
    if not ids:
        return 0
    now = utc_now() if downloaded else ""
    updated = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for chunk in _chunks(ids, 400):
                marks = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE galleries SET downloaded = ?, downloaded_at = ? "
                    f"WHERE gallery_id IN ({marks})",
                    [1 if downloaded else 0, now] + list(chunk),
                )
                updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
        finally:
            conn.close()
    return updated


def set_hidden(gallery_ids: Sequence[int], hidden: bool, path: str | Path | None = None) -> int:
    """逻辑删除：标记隐藏的作品不展示、不下载（爬取到已存在的照样跳过）。"""
    ids = [_optional_int(gid) or 0 for gid in gallery_ids if _optional_int(gid) or 0]
    if not ids:
        return 0
    value = 1 if hidden else 0
    updated = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for chunk in _chunks(ids, 400):
                marks = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE galleries SET hidden = ? WHERE gallery_id IN ({marks})",
                    [value] + list(chunk),
                )
                updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
        finally:
            conn.close()
    return updated



def set_site_favorite(gallery_ids: Sequence[int], favorite: bool,
                      path: str | Path | None = None) -> int:
    """批量设置「站点收藏」标记（用于收藏同步/收藏页本地过滤）。"""
    ids = [gid for gid in (_optional_int(value) or 0 for value in gallery_ids) if gid > 0]
    if not ids:
        return 0
    value = 1 if favorite else 0
    updated = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for chunk in _chunks(ids, 400):
                marks = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"UPDATE galleries SET site_favorite = ? WHERE gallery_id IN ({marks})",
                    [value] + list(chunk),
                )
                updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
        finally:
            conn.close()
    return updated


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


def create_job(gallery_id: int, *, title: str = "", fmt: str = "zip",
               source: str = "manual", batch_id: int | None = None,
               username: str = "", origin: str = "",
               path: str | Path | None = None) -> int:
    """把一个下载任务放进队列，返回 job_id。

    同一个作品 + 同一个格式，如果已经在队列里（queued/running），直接复用，避免重复排队。

    任务行里保存 username / origin，worker 用它们找到该用户的库与站点地址；
    API Key 不落库，只在队列进程内存里跟着任务走。
    """
    gid = _optional_int(gallery_id) or 0
    if gid <= 0:
        raise ValueError("无效的 gallery_id")
    now = utc_now()
    name = (username or "").strip()
    org = (origin or "").strip()
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            existing = conn.execute(
                "SELECT job_id FROM download_jobs WHERE gallery_id = ? AND format = ? "
                "AND status IN ('queued', 'running') ORDER BY job_id LIMIT 1",
                (gid, fmt),
            ).fetchone()
            if existing:
                return int(existing[0])
            cursor = conn.execute(
                "INSERT INTO download_jobs (username, origin, gallery_id, title, format, "
                "source, batch_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (name, org, gid, title or "", fmt, source, batch_id, now),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()


def update_job(job_id: int, path: str | Path | None = None, **fields) -> None:
    """更新任务字段（状态、进度、错误等）。"""
    allowed = {"status", "total_bytes", "done_bytes", "file_path", "error",
               "started_at", "finished_at", "title"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [int(job_id)]
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.execute(f"UPDATE download_jobs SET {assignments} WHERE job_id = ?", values)
            conn.commit()
        finally:
            conn.close()


def get_job(job_id: int, path: str | Path | None = None) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM download_jobs WHERE job_id = ?", (int(job_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(path: str | Path | None = None, limit: int = 100) -> list[dict]:
    """列出下载任务：正在跑/排队在前，其余按时间倒序。"""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM download_jobs "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
            "job_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_next_job(path: str | Path | None = None) -> dict | None:
    """原子地取出一个排队任务并标记为 running（供 worker 调用）。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT * FROM download_jobs WHERE status = 'queued' ORDER BY job_id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE download_jobs SET status = 'running', started_at = ? WHERE job_id = ?",
                (utc_now(), int(row["job_id"])),
            )
            conn.commit()
            claimed = dict(row)
            claimed["status"] = "running"
            return claimed
        finally:
            conn.close()


def cancel_job(job_id: int, path: str | Path | None = None) -> bool:
    """取消排队中的任务；已在跑的任务交给 worker 自行收尾。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (utc_now(), int(job_id)),
            )
            conn.commit()
            return bool(cursor.rowcount)
        finally:
            conn.close()


def cancel_queued_jobs(path: str | Path | None = None) -> int:
    """取消所有排队中的任务（不影响正在下载的）。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'cancelled', finished_at = ? "
                "WHERE status = 'queued'",
                (utc_now(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()


def reset_stale_jobs(path: str | Path | None = None) -> int:
    """进程重启后，把上次遗留的 running 任务重新放回队列。"""
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            cursor = conn.execute(
                "UPDATE download_jobs SET status = 'queued', started_at = NULL, done_bytes = 0 "
                "WHERE status = 'running'"
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()


def get_title(gallery_id: int, path: str | Path | None = None, fallback: str = "") -> str:
    """取作品标题（优先简洁标题），用于生成下载文件名。"""
    gid = _optional_int(gallery_id) or 0
    if gid <= 0:
        return fallback
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT title_pretty, title FROM galleries WHERE gallery_id = ?", (gid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return fallback
    value = (row["title_pretty"] or row["title"] or "").strip()
    return value or fallback


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


def list_batches(path: str | Path | None = None, limit: int = 100) -> list[dict]:
    """抓取批次列表：批次号、来源 URL、时间、条目数。"""
    conn = connect(path)
    try:
        runs = [dict(row) for row in conn.execute(
            "SELECT run_id, url, status, started_at, finished_at, pages_done, "
            "items_seen, items_new, items_dup FROM scrape_runs ORDER BY run_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )]
        counts: dict[int, int] = {}
        for row in conn.execute(
            "SELECT batch_id, COUNT(*) AS n FROM galleries WHERE batch_id > 0 GROUP BY batch_id"
        ):
            counts[int(row[0])] = int(row[1])
    finally:
        conn.close()
    for run in runs:
        run["works"] = counts.get(int(run["run_id"]), 0)
    return runs


def stats(path: str | Path | None = None) -> dict:
    rows = _fetch_rows(None, path)
    works = len(_cluster_rows(rows, True))
    details = sum(1 for row in rows if (row["detail_fetched_at"] or ""))
    covers = sum(1 for row in rows if (row["cover_file"] or ""))
    want = sum(1 for row in rows if (row["want_download"] or 0))
    done = sum(1 for row in rows if (row["downloaded"] or 0))
    site_fav = sum(1 for row in rows if (row["site_favorite"] or 0))
    conn = connect(path)
    try:
        hidden_count = int(conn.execute(
            "SELECT COUNT(*) FROM galleries WHERE hidden = 1").fetchone()[0])
    finally:
        conn.close()
    return {
        "entries": len(rows),
        "hidden": hidden_count,
        "works": works,
        "duplicates": max(0, len(rows) - works),
        "details": details,
        "covers": covers,
        "want_download": want,
        "downloaded": done,
        "site_favorites": site_fav,
        "last_run": last_run(path),
    }


def create_run(url: str, path: str | Path | None = None) -> int:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            cursor = conn.execute(
                "INSERT INTO scrape_runs (url, status, started_at) VALUES (?, 'running', ?)",
                (url, utc_now()),
            )
            conn.commit()
            return int(cursor.lastrowid)
    finally:
        conn.close()


def finish_run(
    run_id: int,
    *,
    status: str,
    pages_total: int = 0,
    pages_done: int = 0,
    items_seen: int = 0,
    items_new: int = 0,
    items_dup: int = 0,
    error: str | None = None,
    path: str | Path | None = None,
) -> None:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            conn.execute(
                """UPDATE scrape_runs SET status = ?, finished_at = ?, pages_total = ?,
                   pages_done = ?, items_seen = ?, items_new = ?, items_dup = ?, error = ?
                   WHERE run_id = ?""",
                (status, utc_now(), pages_total, pages_done, items_seen, items_new, items_dup, error, run_id),
            )
            conn.commit()
    finally:
        conn.close()


def last_run(path: str | Path | None = None) -> dict | None:
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM scrape_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()




def latest_run_for_url(url: str, path: str | Path | None = None) -> dict | None:
    """返回该列表地址最近一次成功抓取的批次记录（用于增量抓取判断）。"""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM scrape_runs WHERE url = ? AND status = 'done' "
            "ORDER BY run_id DESC LIMIT 1",
            (str(url),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_run(run_id: int, path: str | Path | None = None) -> bool:
    """删除一次爬取记录（用于「爬取结果为 0 时不保留该批次号」）。"""
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            cursor = conn.execute("DELETE FROM scrape_runs WHERE run_id = ?", (int(run_id),))
            conn.commit()
            return bool(cursor.rowcount)
    finally:
        conn.close()


def merge_runs_for_url(url: str, keep_run_id: int,
                       path: str | Path | None = None) -> int:
    """同一 URL 只保留最新一次批次。

    删除该 URL 的其它批次记录，并把旧批次的条目归属改到最新批次，
    便于「保存本批全部数据」拿到该地址的最新完整清单。返回清理的旧批次数量。
    """
    keep = int(keep_run_id or 0)
    if keep <= 0:
        return 0
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            old_ids = [int(row[0]) for row in conn.execute(
                "SELECT run_id FROM scrape_runs WHERE url = ? AND run_id <> ?",
                (str(url), keep),
            )]
            if not old_ids:
                return 0
            for chunk in _chunks(old_ids, 400):
                marks = ",".join("?" for _ in chunk)
                conn.execute(
                    f"UPDATE galleries SET batch_id = ? WHERE batch_id IN ({marks})",
                    [keep] + list(chunk),
                )
                conn.execute(f"DELETE FROM scrape_runs WHERE run_id IN ({marks})", list(chunk))
            conn.commit()
            return len(old_ids)
    finally:
        conn.close()


def reset_all(path: str | Path | None = None) -> dict:
    conn = connect(path)
    try:
        with _WRITE_LOCK:
            galleries = conn.execute("SELECT COUNT(*) FROM galleries").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
            conn.execute("DELETE FROM galleries")
            conn.execute("DELETE FROM scrape_runs")
            conn.commit()
        return {"galleries": int(galleries), "runs": int(runs)}
    finally:
        conn.close()

# ============================================================
# 多用户：账号、会话与用户独立数据文件
# ============================================================

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
# 云端同步用的全量行导出 / 合并写入
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


def all_gallery_rows(path: str | Path | None = None) -> list[dict]:
    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM galleries").fetchall()]
    finally:
        conn.close()


def all_run_rows(path: str | Path | None = None) -> list[dict]:
    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM scrape_runs").fetchall()]
    finally:
        conn.close()


def row_updated_at(row: dict, table: str) -> str:
    """同步用的「最后修改时间」：作品用 last_seen，批次用 finished_at/started_at。"""
    if table == "galleries":
        return str(row.get("last_seen") or "")
    return str(row.get("finished_at") or row.get("started_at") or "")


def upsert_gallery_rows_from_cloud(rows: Iterable[dict], path: str | Path | None = None) -> int:
    """把云端作品行合并进本地（需求：标记字段 1 覆盖 0）。

    - 新 gallery_id：直接 INSERT。
    - 已存在：标记字段（want_download/downloaded/hidden/site_favorite）
      ``max(本地, 云端)`` 合并 —— 任一方为 1 则结果 1（done 覆盖 todo，
      逻辑删除覆盖未删除），不受 last_seen 影响。
    - 内容字段：仅当 ``excluded.last_seen >= galleries.last_seen`` 时覆盖。
    - first_seen 取较旧、last_seen 取较新。

    返回本次处理的云端行数。
    """
    items = [r for r in rows if _optional_int(r.get("gallery_id"))]
    if not items:
        return 0
    cols = ", ".join(_GALLERY_COLUMNS)
    marks = ", ".join("?" for _ in _GALLERY_COLUMNS)
    mark_set = ", ".join(
        f"{c} = max(galleries.{c}, excluded.{c})" for c in _GALLERY_MARK_COLUMNS)
    content_set = ", ".join(
        f"{c} = CASE WHEN excluded.last_seen >= galleries.last_seen "
        f"THEN excluded.{c} ELSE galleries.{c} END"
        for c in _GALLERY_CONTENT_COLUMNS)
    time_set = (
        "first_seen = CASE WHEN excluded.first_seen < galleries.first_seen "
        "THEN excluded.first_seen ELSE galleries.first_seen END, "
        "last_seen = CASE WHEN excluded.last_seen >= galleries.last_seen "
        "THEN excluded.last_seen ELSE galleries.last_seen END")
    updates = ", ".join(p for p in (mark_set, content_set, time_set) if p)
    sql = (
        f"INSERT INTO galleries ({cols}) VALUES ({marks}) "
        f"ON CONFLICT(gallery_id) DO UPDATE SET {updates}"
    )
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            conn.executemany(sql, [
                tuple(_optional_int(r.get(c)) if c in _GALLERY_INT_COLUMNS else r.get(c)
                      for c in _GALLERY_COLUMNS)
                for r in items
            ])
            conn.commit()
        finally:
            conn.close()
    return len(items)


def upsert_run_rows_from_cloud(rows: Iterable[dict], path: str | Path | None = None) -> int:
    """把云端批次行合并进本地：按 url 归并，时间较新者胜，返回改动行数。

    `scrape_runs` 没有 url 唯一约束（历史库中同一地址可能残留多条批次），
    所以这里逐条比对而不是依赖 ON CONFLICT。云端行可携带 run_id：
    本地还没有该 url 时按原 run_id 落库，保证 galleries.batch_id 的关联仍然成立；
    若该 run_id 已被别的地址占用，则改用自增 id，并把指向旧 id 的作品改指过来。
    """
    items = [r for r in rows if str(r.get("url") or "").strip()]
    if not items:
        return 0
    changed = 0
    with _WRITE_LOCK:
        conn = connect(path)
        try:
            for r in items:
                url = str(r.get("url") or "").strip()
                cloud_ts = str(r.get("finished_at") or r.get("started_at") or "")
                row = conn.execute(
                    "SELECT run_id, started_at, finished_at FROM scrape_runs "
                    "WHERE url = ? ORDER BY run_id DESC LIMIT 1",
                    (url,),
                ).fetchone()
                values = (
                    str(r.get("status") or ""),
                    str(r.get("started_at") or ""),
                    str(r.get("finished_at") or "") or None,
                    int(r.get("pages_total") or 0),
                    int(r.get("pages_done") or 0),
                    int(r.get("items_seen") or 0),
                    int(r.get("items_new") or 0),
                    int(r.get("items_dup") or 0),
                    r.get("error"),
                )
                if row is None:
                    run_id = _optional_int(r.get("run_id"))
                    remap_from = run_id
                    if run_id is not None and conn.execute(
                        "SELECT 1 FROM scrape_runs WHERE run_id = ?", (run_id,)
                    ).fetchone():
                        run_id = None
                    cur = conn.execute(
                        "INSERT INTO scrape_runs (run_id, url, status, started_at, finished_at,"
                        " pages_total, pages_done, items_seen, items_new, items_dup, error)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, url) + values,
                    )
                    if run_id is None and remap_from is not None:
                        # 原 run_id 被占用：把引用它的作品改指到新插入的批次
                        conn.execute("UPDATE galleries SET batch_id = ? WHERE batch_id = ?",
                                     (int(cur.lastrowid), int(remap_from)))
                    changed += 1
                else:
                    local_ts = str(row["finished_at"] or row["started_at"] or "")
                    if cloud_ts and cloud_ts > local_ts:
                        conn.execute(
                            "UPDATE scrape_runs SET status = ?, started_at = ?, finished_at = ?,"
                            " pages_total = ?, pages_done = ?, items_seen = ?, items_new = ?,"
                            " items_dup = ?, error = ? WHERE run_id = ?",
                            values + (int(row["run_id"]),),
                        )
                        changed += 1
            conn.commit()
        finally:
            conn.close()
    return changed


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
