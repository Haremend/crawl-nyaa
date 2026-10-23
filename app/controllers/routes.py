"""路由表：(方法, 路径) → controller 函数。这是 findability 的核心交付物——
一个文件看到全部 API 端点到 controller 的映射。

dispatch(h, method, path, query) 查表调用；静态文件/404 由 Handler 处理。
controller 函数签名：GET 带 (h, query)，POST 带 (h)，远程端点带 (h, query)。
"""
from __future__ import annotations

from app.controllers import (auth, assets, download, favorites, scrape,
                             settings, sync, works)

# (METHOD, PATH) -> controller callable。GET 的 callable 收 (h, query)，
# POST 的收 (h)，远程端点 GET/POST 收 (h, query) 或 (h)。
_ROUTES = {}


def _route(method: str, path: str):
    def wrap(fn):
        _ROUTES[(method, path)] = fn
        return fn
    return wrap


# ---------- auth ----------
_route("GET", "/api/auth/me")(auth.get_auth_me)
_route("POST", "/api/auth/register")(auth.post_register)
_route("POST", "/api/auth/login")(auth.post_login)
_route("POST", "/api/auth/logout")(auth.post_logout)

# ---------- settings / credentials ----------
_route("GET", "/api/settings/credentials")(settings.get_credentials)
_route("POST", "/api/settings/credentials")(settings.post_credentials)
_route("POST", "/api/settings/site")(settings.post_settings_site)
_route("POST", "/api/settings/proxy")(settings.post_settings_proxy)

# ---------- meta / works / batches / marks ----------
_route("GET", "/api/meta")(works.get_meta)
_route("GET", "/api/works")(works.get_works)
_route("GET", "/api/batches")(works.get_batches)
_route("GET", "/api/site-favorites")(works.get_site_favorites)
_route("POST", "/api/want-download")(works.post_want_download)
_route("POST", "/api/hidden")(works.post_hidden)
_route("POST", "/api/downloaded")(works.post_downloaded)
_route("POST", "/api/reset")(download.post_reset)

# ---------- sync / remote ----------
_route("GET", "/api/sync/status")(sync.get_sync_status)
_route("POST", "/api/sync")(sync.post_sync)
_route("POST", "/api/sync/pull")(sync.post_sync_pull)
_route("POST", "/api/sync/push")(sync.post_sync_push)
_route("POST", "/api/sync/cross")(sync.post_sync_cross)
_route("GET", "/api/remote/account")(sync.get_remote_account)
_route("POST", "/api/remote/account")(sync.post_remote_account)
_route("POST", "/api/remote/sync")(sync.post_remote_sync)

# ---------- scrape ----------
_route("POST", "/api/scrape")(scrape.post_scrape)
_route("POST", "/api/scrape/cancel")(scrape.post_scrape_cancel)
_route("GET", "/api/scrape/status")(scrape.get_scrape_status)

# ---------- download ----------
_route("GET", "/api/download/targets")(download.get_download_targets)
_route("GET", "/api/download/jobs")(download.get_download_jobs)
_route("GET", "/api/download/status")(download.get_download_status)
_route("GET", "/api/download/file")(download.get_download_file)
_route("POST", "/api/download-url")(download.post_download_url)
_route("POST", "/api/batch/save")(download.post_batch_save)
_route("POST", "/api/download/start")(download.post_download_start)
_route("POST", "/api/download/cancel")(download.post_download_cancel)
_route("POST", "/api/download/enqueue")(download.post_download_enqueue)
_route("POST", "/api/download/jobs/cancel")(download.post_download_jobs_cancel)

# ---------- favorites ----------
_route("POST", "/api/favorites/sync")(favorites.post_favorites_sync)

# ---------- assets ----------
_route("GET", "/api/thumb")(assets.get_thumb)
_route("GET", "/api/export.csv")(assets.get_export_csv)


def known(method: str, path: str) -> bool:
    return (method, path) in _ROUTES


def dispatch(h, method: str, path: str, query: dict) -> None:
    """按 (method, path) 查表调用对应 controller。未命中 → 404。"""
    fn = _ROUTES.get((method, path))
    if fn is None:
        h._error("未知路径" if method == "GET" else "未知接口", 404)
        return
    # 远程端点与 GET-with-query 走 (h, query)；纯 POST 走 (h)
    import inspect
    params = inspect.signature(fn).parameters
    if len(params) > 1:
        fn(h, query)
    else:
        fn(h)
