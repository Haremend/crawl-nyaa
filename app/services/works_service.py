"""作品列表与清空：cloud↔local 分支 + 展示字段装饰。

从 app/server.py 的 _handle_works / _handle_reset / _decorate 抽出。
controller 解析好 query 参数后把 filters dict 传进来；本模块按存储模式
分叉到 cloud_service（云）或 db.query_works（本地），再逐条 _decorate
（补缩略图代理路径与本地封面路径——复用 server.py 的模块级工具函数，
经 app.server 顶层导入，避免 server↔service 循环 import）。
"""
from __future__ import annotations

from app import config, db
from app.services import cloud_service


def _empty_query(filters: dict) -> dict:
    """controller 需要回 401/502 时的空结果兜底（与 _handle_works 原兜底同 shape）。"""
    return {"items": [], "total": 0, "page": 1, "pages": 1,
            "per_page": 1, "raw_total": 0, "unique_total": 0,
            "language_stats": [], "dedupe": filters.get("dedupe_title", False),
            "sort": filters.get("sort", "recent")}


def _decorate(item: dict) -> dict:
    """给作品补上缩略图代理路径与本地封面路径。

    thumb_proxy_path / local_cover_path 是 app/server.py 的模块级工具函数；
    controller 层在调用本模块前已把 server 实例在手上，这里不依赖它——
    直接复用同名工具（顶层 import 时 app.server 已完成加载，无循环）。
    """
    from app.server import thumb_proxy_path, local_cover_path
    item["thumb"] = thumb_proxy_path(item.get("thumbnail") or "")
    item["cover_url"] = local_cover_path(item.get("cover_file") or "")
    for variant in item.get("variants", []):
        variant["thumb"] = thumb_proxy_path(variant.get("thumbnail") or "")
        variant["cover_url"] = local_cover_path(variant.get("cover_file") or "")
    return item


def list_works(filters: dict, user: dict | None, user_db: str | None = None) -> dict:
    """/api/works 的 cloud↔local 分叉 + decorate。

    filters 由 controller 从 query 解析（q/languages/dedupe_title/sort/page/
    per_page/uncensored/want_download_only/downloaded_only/site_favorites_only/
    batch_id）。返回 shape 与 _handle_works 的 _json(data) 一致（controller
    最后补 data["ok"]=True）。

    云模式下无 username / Supabase 失败时返回带 error+status 的结构（与原
    handler 的 401/502 兜底同 shape，由 controller 映射到 _error）。
    """
    username = user.get("username", "") if user else ""
    if config.storage_is_cloud():
        data = cloud_service.query_works(username, filters)
        if "error" in data:  # 云模式必须登录，controller 映射 401
            return data
        # SupabaseError 直接抛给 controller（对应原 handler 的 502 兜底）
    else:
        data = db.query_works(
            q=filters.get("q"), languages=filters.get("languages"),
            dedupe_title=filters.get("dedupe_title", True),
            sort=filters.get("sort", "recent"),
            page=filters.get("page", 1), per_page=filters.get("per_page", 24),
            uncensored=filters.get("uncensored", False),
            want_download_only=filters.get("want_download_only", False),
            downloaded_only=filters.get("downloaded_only", False),
            site_favorites_only=filters.get("site_favorites_only", False),
            batch_id=filters.get("batch_id"),
            path=user_db,
        )
    for item in data["items"]:
        _decorate(item)
    return data


def reset(user: dict | None, user_db: str | None = None) -> dict:
    """/api/reset 的 cloud↔local 分叉：云走 supabase_client.reset_user_data，
    本地走 db.reset_all。返回 {"galleries": n, "runs": m}。"""
    if config.storage_is_cloud():
        username = user.get("username", "") if user else ""
        return cloud_service.reset(username)
    return db.reset_all(user_db)
