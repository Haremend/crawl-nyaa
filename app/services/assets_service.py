"""静态资源代理 + 导出：缩略图 SHA1 磁盘缓存代理、CSV 导出。

从 app/server.py 的 _handle_thumb / _handle_export 抽出。

分层约定：HTTP 写（self._send / self._error）留 controller；本层返回
描述符（缩略图：local 盘缓存 / error；CSV：字节串 + 响应头），
controller 拿到后调 _send。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from app import config, db, supabase_client
from app.covers import is_media_url
from app.scraper import FetchError, Fetcher


def proxy_thumb(url: str) -> dict:
    """远端缩略图代理（SHA1 磁盘缓存）。返回描述符：
    - 命中/回源成功：{"kind": "local", "body": bytes, "mime": str,
      "cache_headers": dict}
    - 非法 URL：{"kind": "error", "status": 400, "message"}
    - 回源失败：{"kind": "error", "status": 502, "message"}
    - 超大小上限：{"kind": "error", "status": 413, "message"}
    """
    raw = (url or "").strip()
    if not is_media_url(raw):
        return {"kind": "error", "status": 400,
                "message": "只允许 http/https 图片地址"}
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    cache_path = config.THUMB_CACHE_DIR / f"{digest}.bin"
    meta_path = config.THUMB_CACHE_DIR / f"{digest}.json"
    if cache_path.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {"kind": "local", "body": cache_path.read_bytes(),
                    "mime": meta.get("content_type") or "image/jpeg",
                    "cache_headers": {"Cache-Control": "public, max-age=86400"}}
        except (OSError, ValueError):
            pass
    try:
        body, content_type = Fetcher().fetch_bytes(raw, referer=None)
    except FetchError as exc:
        return {"kind": "error", "status": 502, "message": str(exc)}
    if len(body) > config.THUMB_MAX_BYTES:
        return {"kind": "error", "status": 413, "message": "图片超过大小上限"}
    try:
        config.THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        meta_path.write_text(
            json.dumps({"content_type": content_type, "url": raw},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return {"kind": "local", "body": body, "mime": content_type,
            "cache_headers": {"Cache-Control": "public, max-age=86400"}}


def export_csv(params: dict, user: dict | None, user_db: Path,
               cloud_mode: bool) -> tuple[bytes, dict]:
    """按当前筛选导出 CSV（UTF-8 BOM）。返回 (body_bytes, response_headers)。

    params 为 controller 解析好的筛选 dict（q / languages / dedupe / sort /
    uncensored / want / site_favorites），与 server._handle_export 解析结果同键。
    """
    q = params.get("q")
    languages = params.get("languages") or []
    dedupe = params.get("dedupe", True)
    sort = params.get("sort") or "recent"
    uncensored = params.get("uncensored", False)
    want_only = params.get("want", False)
    site_only = params.get("site_favorites", False)

    items: list[dict] = []
    page = 1
    while len(items) < config.EXPORT_MAX_ROWS:
        if cloud_mode:
            # 与 server._cloud_query_works 同源的云端作品查询（2b 的
            # cloud_service 建好后此处可切换复用；行为等价）
            username = (user.get("username", "") if user else "")
            rows = [dict(r) for r in
                    supabase_client.cloud_fetch_gallery_rows(username)]
            data = _cloud_filter(rows, q=q, languages=languages, dedupe=dedupe,
                                 sort=sort, page=page, per_page=200,
                                 uncensored=uncensored, want_only=want_only,
                                 site_only=site_only)
        else:
            data = db.query_works(
                q=q, languages=languages, dedupe_title=dedupe, sort=sort,
                page=page, per_page=200, uncensored=uncensored,
                want_download_only=want_only, site_favorites_only=site_only,
                path=user_db,
            )
        items.extend(data["items"])
        if page >= data["pages"]:
            break
        page += 1

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["gallery_id", "all_variant_ids", "title", "title_original",
                     "cover_file", "uncensored", "languages", "artists", "groups",
                     "parodies", "tags", "num_pages", "num_favorites",
                     "upload_date", "scanlator", "url", "first_seen", "last_seen"])
    for item in items:
        writer.writerow([
            item["gallery_id"],
            " ".join(str(value) for value in item["ids"]),
            item["title"],
            item["title_original"],
            item["cover_file"],
            1 if item["uncensored"] else 0,
            " ".join(lang["key"] for lang in item["languages"]),
            " ".join(item["artists"]),
            " ".join(item["groups"]),
            " ".join(item["parodies"]),
            " ".join(item["tags"]),
            item["num_pages"] if item["num_pages"] is not None else "",
            item["num_favorites"] if item["num_favorites"] is not None else "",
            item["upload_date"],
            item["scanlator"],
            item["url"],
            item["first_seen"],
            item["last_seen"],
        ])
    body = ("﻿" + buffer.getvalue()).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="gallery_works.csv"',
        "Cache-Control": "no-store",
    }
    return body, headers


def _cloud_filter(rows: list[dict], *, q, languages, dedupe, sort, page,
                  per_page, uncensored, want_only, site_only) -> dict:
    """云端作品行内存过滤/去重/排序/分页（与 server._cloud_query_works 同规则，
    仅服务 CSV 导出场景；复杂字段（封面/批次）由 2b 的 cloud_service 覆盖）。"""
    from app.dao.clustering import _cluster_rows, _serialize_group, _sort_items

    row_dicts = [dict(r) for r in rows]
    wanted = [str(v).strip().lower() for v in (languages or []) if str(v).strip()]
    groups = _cluster_rows(row_dicts, dedupe)
    items = [_serialize_group(group, wanted) for group in groups]
    if wanted:
        ws = set(wanted)
        items = [it for it in items if ws.intersection(it["language_keys"])]
    if uncensored:
        items = [it for it in items if it["uncensored"]]
    if want_only:
        items = [it for it in items if it["want_download"]]
    if site_only:
        items = [it for it in items if it["site_favorite"]]
    if q:
        kw = str(q)
        items = [
            it for it in items
            if (kw in it.get("title", "") or kw in it.get("title_original", "")
                or any(kw in t for t in it.get("tags", []))
                or any(kw in a for a in it.get("artists", []))
                or any(kw in g for g in it.get("groups", [])))
        ]
    items = _sort_items(items, sort or "recent")
    total = len(items)
    per_page = max(1, min(int(per_page or 24), 200))
    pages = max(1, -(-total // per_page)) if total else 1
    page_no = max(1, min(int(page or 1), pages))
    start = (page_no - 1) * per_page
    return {"items": items[start:start + per_page], "total": total,
            "page": page_no, "pages": pages}
