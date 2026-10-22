"""DAO 聚类层：读画廊行 + 同作品去重（原始标题精确分组 + 标题指纹模糊合并）
+ 选版/序列化/排序。全是纯函数（对 sqlite3.Row / dict 操作），无写库、无锁。

``galleries.query_works`` / ``runs.stats`` / ``galleries.list_batch_targets`` 等
都从这里取 ``_fetch_rows`` / ``_cluster_rows`` / ``_serialize_group`` / ``_sort_items``。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Iterable, Sequence

from .. import config, titles
from .schema import connect


def _row_list(row: sqlite3.Row, key: str) -> list:
    try:
        value = json.loads(row[key] or "[]")
    except (ValueError, TypeError, IndexError):
        return []
    return value if isinstance(value, list) else []


def _fetch_rows(q: str | None = None, path: str | None = None) -> list[sqlite3.Row]:
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


def _language_order(langs: Iterable, preferred: Sequence[str]) -> list[str]:
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
