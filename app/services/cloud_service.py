"""云模式业务逻辑：作品查询 / 批次 / 统计 / 标题 / 下载目标 / 标记 / 清空。

从 app/server.py 的 _cloud_query_works / _cloud_title / _cloud_batches /
_cloud_stats / _cloud_want_targets / _cloud_batch_targets / _cloud_mark /
_cloud_reset / _site_origin 抽出。每个函数返回结构化数据，controller 负责
HTTP 格式化（_json/_error）。行为与拆分前完全一致。

分层约定：本模块不再直接发裸 SQL——清空走 supabase_client.reset_user_data
（封装原先 server 里 import supabase_client as _s 发 DELETE 的那段）。
"""
from __future__ import annotations

from app import config, db, supabase_client


def _username(user: dict | None) -> str:
    return user.get("username", "") if user else ""


def _empty_query(filters) -> dict:
    return {"items": [], "total": 0, "page": 1, "pages": 1,
            "per_page": 1, "raw_total": 0, "unique_total": 0,
            "language_stats": [], "dedupe": False, "sort": "recent"}


def query_works(username: str, filters: dict) -> dict:
    """云模式作品查询：拉云端作品行，内存里复用 db 的去重/选版/排序。

    filters 键：q / languages / dedupe_title / sort / page / per_page /
    uncensored / want_download_only / downloaded_only / site_favorites_only /
    batch_id。返回与 _handle_works 的 _json({...}) 完全同 shape。
    Supabase 异常向上抛，controller try/except 转 _error。
    """
    username = str(username or "").strip()
    if not username:
        # 云模式必须登录：controller 应已 401，这里是防御性兜底
        result = _empty_query(filters)
        result["error"] = "云端模式必须先登录"
        result["status"] = 401
        return result
    rows = supabase_client.cloud_fetch_gallery_rows(username)
    if not rows:
        return {
            "items": [], "total": 0, "page": 1, "pages": 1,
            "per_page": int(filters.get("per_page") or 24),
            "raw_total": 0, "unique_total": 0,
            "dedupe": filters.get("dedupe_title", True),
            "sort": filters.get("sort", "recent"), "language_stats": [],
        }
    # db 的聚类/序列化/排序只依赖 row[key] 按键访问，普通 dict 即可复用
    row_dicts = [dict(r) for r in rows]
    wanted = [str(v).strip().lower() for v in (filters.get("languages") or [])
              if str(v).strip()]
    groups = db._cluster_rows(row_dicts, filters.get("dedupe_title", True))
    items = [db._serialize_group(group, wanted) for group in groups]
    counter: dict[str, int] = {}
    for it in items:
        for key in it["language_keys"]:
            counter[key] = counter.get(key, 0) + 1
    language_stats = [
        {"key": k, "label": config.language_label(k), "count": c}
        for k, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if wanted:
        ws = set(wanted)
        items = [it for it in items if ws.intersection(it["language_keys"])]
    if filters.get("uncensored"):
        items = [it for it in items if it["uncensored"]]
    if filters.get("want_download_only"):
        items = [it for it in items if it["want_download"]]
    if filters.get("downloaded_only"):
        items = [it for it in items if it["downloaded"]]
    if filters.get("pending_only"):
        items = [it for it in items if not it["want_download"] and not it["downloaded"]]
    if filters.get("site_favorites_only"):
        items = [it for it in items if it["site_favorite"]]
    if filters.get("batch_id"):
        items = [it for it in items if it["batch_id"] == int(filters["batch_id"])]
    if filters.get("q"):
        kw = str(filters["q"])
        items = [
            it for it in items
            if (kw in it.get("title", "") or kw in it.get("title_original", "")
                or any(kw in t for t in it.get("tags", []))
                or any(kw in a for a in it.get("artists", []))
                or any(kw in g for g in it.get("groups", [])))
        ]
    items = db._sort_items(items, filters.get("sort") or "recent")
    total = len(items)
    per_page = max(1, min(int(filters.get("per_page") or 24), 200))
    pages = max(1, -(-total // per_page)) if total else 1
    page_no = max(1, min(int(filters.get("page") or 1), pages))
    start = (page_no - 1) * per_page
    return {
        "items": items[start:start + per_page],
        "total": total, "page": page_no, "pages": pages, "per_page": per_page,
        "raw_total": len(row_dicts), "unique_total": len(groups),
        "dedupe": filters.get("dedupe_title", True),
        "sort": filters.get("sort", "recent"),
        "language_stats": language_stats,
    }


def title(username: str, gallery_id: int) -> str:
    """云模式取作品标题：title_pretty 优先，缺则 title。"""
    username = str(username or "").strip()
    if not username:
        return ""
    try:
        for r in supabase_client.cloud_fetch_gallery_rows(username):
            if int(r.get("gallery_id") or 0) == int(gallery_id):
                return (str(r.get("title_pretty") or "").strip()
                        or str(r.get("title") or "").strip())
    except supabase_client.SupabaseError:
        return ""
    return ""


def batches(username: str) -> list[dict]:
    """云模式批次列表：从 claw_scrape_runs 拉该用户批次，统计每批作品数。"""
    username = str(username or "").strip()
    if not username:
        return []
    try:
        runs = supabase_client.cloud_fetch_run_rows(username)
    except supabase_client.SupabaseError:
        return []
    # 每批作品数：按 batch_id 统计该用户画廊行
    try:
        rows = supabase_client.cloud_fetch_gallery_rows(username)
        counts: dict[int, int] = {}
        for r in rows:
            bid = int(r.get("batch_id") or 0)
            if bid > 0:
                counts[bid] = counts.get(bid, 0) + 1
    except supabase_client.SupabaseError:
        counts = {}
    result = []
    for run in sorted(runs, key=lambda r: int(r.get("run_id") or 0), reverse=True)[:100]:
        run = dict(run)
        run["works"] = counts.get(int(run.get("run_id") or 0), 0)
        result.append(run)
    return result


def stats(username: str) -> dict:
    """云模式库统计：作品数 / 待下载 / 已下载 / 隐藏等（从云端画廊行算）。"""
    username = str(username or "").strip()
    if not username:
        return {"entries": 0, "hidden": 0, "works": 0, "duplicates": 0,
                "details": 0, "covers": 0, "want_download": 0, "downloaded": 0,
                "site_favorites": 0, "last_run": None}
    try:
        rows = supabase_client.cloud_fetch_gallery_rows(username)
    except supabase_client.SupabaseError:
        rows = []
    entries = len(rows)
    hidden = sum(1 for r in rows if r.get("hidden"))
    want = sum(1 for r in rows if r.get("want_download"))
    done = sum(1 for r in rows if r.get("downloaded"))
    site_fav = sum(1 for r in rows if r.get("site_favorite"))
    details = sum(1 for r in rows if r.get("detail_fetched_at"))
    covers = sum(1 for r in rows if r.get("cover_file"))
    # last_run：最近一个批次
    try:
        runs = supabase_client.cloud_fetch_run_rows(username)
        last = max(runs, key=lambda r: int(r.get("run_id") or 0), default=None)
        last = dict(last) if last else None
    except supabase_client.SupabaseError:
        last = None
    return {"entries": entries, "hidden": hidden, "works": entries,
            "duplicates": 0, "details": details, "covers": covers,
            "want_download": want, "downloaded": done,
            "site_favorites": site_fav, "last_run": last}


def want_targets(username: str, *, skip_downloaded: bool) -> list[dict]:
    """云模式：从云端作品行算「待下载」目标（组内任一 want_download 即命中，
    取推荐版本）。"""
    username = str(username or "").strip()
    if not username:
        return []
    rows = [dict(r) for r in supabase_client.cloud_fetch_gallery_rows(username)
            if not r.get("hidden")]
    if not rows:
        return []
    targets: list[dict] = []
    for group in db._cluster_rows(rows, True):
        if not any(r.get("want_download") for r in group):
            continue
        if skip_downloaded and any(r.get("downloaded") for r in group):
            continue
        ordered = sorted(group, key=lambda r: db._variant_rank(r, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main.get("title_pretty") or main.get("title") or ""),
        })
    targets.sort(key=lambda t: -t["gallery_id"])
    return targets


def batch_targets(username: str, batch_id: int, *, skip_downloaded: bool) -> list[dict]:
    """云模式：从云端作品行算指定批次目标。"""
    username = str(username or "").strip()
    if not username:
        return []
    rows = [dict(r) for r in supabase_client.cloud_fetch_gallery_rows(username)
            if not r.get("hidden") and int(r.get("batch_id") or 0) == int(batch_id)]
    if not rows:
        return []
    targets: list[dict] = []
    for group in db._cluster_rows(rows, True):
        if skip_downloaded and any(r.get("downloaded") for r in group):
            continue
        ordered = sorted(group, key=lambda r: db._variant_rank(r, []))
        main = ordered[0]
        targets.append({
            "gallery_id": int(main["gallery_id"]),
            "title": (main.get("title_pretty") or main.get("title") or ""),
        })
    targets.sort(key=lambda t: -t["gallery_id"])
    return targets


def mark_flags(username: str, flag_col: str, gallery_ids, value: bool) -> int:
    """云模式：把标记直接写进云端作品行（按当前用户隔离）。"""
    username = str(username or "").strip()
    if not username:
        return 0
    return supabase_client.cloud_mark_gallery_flags(
        username, gallery_ids, flag_col=flag_col, value=value)


def reset(username: str) -> dict:
    """云模式清空：删除该用户云端作品与批次（账号/会话保留）。"""
    username = str(username or "").strip()
    if not username:
        return {"galleries": 0, "runs": 0}
    return supabase_client.reset_user_data(username)


def site_origin(user: dict | None, user_db: str | None = None) -> str:
    """当前用户/匿名模式的站点地址。

    云模式：读云端账号行 claw_users.site_origin（注册时已写），读不到再查云端；
    本地模式：读本地库 settings.site_origin（走 db.get_site_origin，经 user_db）。
    """
    if config.storage_is_cloud():
        if user and user.get("site_origin"):
            return user["site_origin"].rstrip("/")
        if user:
            try:
                acc = supabase_client.fetch_account(user["username"])
                if acc and acc.get("site_origin"):
                    return str(acc["site_origin"]).rstrip("/")
            except supabase_client.SupabaseError:
                return ""
            return ""
        return ""
    return db.get_site_origin(user_db)
