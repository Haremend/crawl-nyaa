"""DAO 作品层：入库、详情补全、查询、标记、封面、批次/待下载目标。

依赖基础层 ``schema``（连接/锁/通用工具）与聚类层 ``clustering``（读行+聚类+序列化+排序）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from .schema import (  # noqa: F401 (Path 重导出给类型注解)
    _WRITE_LOCK, _chunks, _optional_int, connect, utc_now,
)
from .clustering import (
    _cluster_rows, _fetch_rows, _serialize_group, _sort_items, _variant_rank,
)

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


def _item_to_row(item: Any) -> dict:
    if hasattr(item, "as_row"):
        return dict(item.as_row())
    return dict(item)


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _json_list(value: Any) -> str:
    return json.dumps([str(entry) for entry in _as_list(value) if str(entry).strip()], ensure_ascii=False)


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
