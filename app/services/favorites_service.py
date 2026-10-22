"""收藏（站点账号）业务逻辑：列表同步进本地库、一键把全部收藏标为待下载。

从 app/server.py 的 _handle_site_favorites / _handle_favorites_sync 抽出。
现状这两处都是**本地模式**路径（cloud 模式无对应分支）——保持现状，不擅自加。

分层约定：Key/origin 校验与 HTTP 响应留 controller；本层做上游拉取、
gallery 行改写、入库与标记。
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app.upstream import UpstreamClient, UpstreamError


def list_site_favorites(api_key: str, user: dict | None, user_db: Path,
                        origin: str, page: int, keyword: str,
                        upstream=None) -> dict:
    """站点收藏列表：拉取 + 同步进本地库，返回 controller 直接 _json 的 dict。

    出错返回 {"ok": False, "error", "status"}；成功返回与拆分前
    _handle_site_favorites 的 self._json({...}) 完全同 shape（items 为
    controller 经 _ordered_by_ids 装饰后的列表，这里返回未装饰的原始
    payload，controller 负责二次装饰 —— 见 server._ordered_by_ids）。
    """
    try:
        payload = upstream(origin, api_key).list_favorites(page=page, query=keyword)
    except UpstreamError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status or 502}

    items = payload.get("items") or []
    for item in items:
        item["site_favorite"] = 1
        item["from_scrape"] = 0
        item["url"] = f"{origin}/g/{item['gallery_id']}/"
    if items:
        # 同步进本地库，这样「待下载/已下载」标记与排序都能复用同一套逻辑
        db.upsert_items(items, source_url=f"{origin}/favorites", path=user_db)
        db.set_site_favorite([item["gallery_id"] for item in items], True, user_db)

    return {
        "ok": True,
        "items": [item["gallery_id"] for item in items],
        "page": payload.get("page", page),
        "num_pages": payload.get("num_pages", 1),
        "per_page": payload.get("per_page", 25),
        "total": payload.get("total", len(items)),
    }


def sync_to_want(api_key: str, user: dict | None, user_db: Path,
                 origin: str, upstream=None) -> dict:
    """遍历收藏所有页，把全部收藏作品在本地标记为「待下载」。"""
    client = upstream(origin, api_key)
    synced = 0
    page = 1
    while True:
        payload_data = client.list_favorites(page=page)
        items = payload_data.get("items") or []
        if not items:
            break
        for item in items:
            item["site_favorite"] = 1
            item["from_scrape"] = 0
            item["url"] = f"{origin}/g/{item['gallery_id']}/"
        if items:
            ids = [item["gallery_id"] for item in items]
            db.upsert_items(items, source_url=f"{origin}/favorites", path=user_db)
            db.set_site_favorite(ids, True, user_db)
        synced += db.set_want_download([item["gallery_id"] for item in items],
                                       True, user_db)
        num_pages = int(payload_data.get("num_pages") or 1)
        if page >= num_pages:
            break
        page += 1
    return {"ok": True, "synced": synced, "pages": page}
