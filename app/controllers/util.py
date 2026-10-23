"""controller 层共用小工具（不碰 Handler 状态）。"""
from __future__ import annotations

_SORTS = ("recent", "oldest", "title", "original", "variants", "favorites",
          "pages", "priority")


def int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    """取 query 里的 int 参数并夹到 [low, high]；缺省/非法用 default。"""
    values = (query or {}).get(name) or []
    raw = values[0] if values else None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _bool_param(query: dict, name: str, default: bool = False) -> bool:
    """query 里的 bool 参数（URLSearchParams 风格：多值取首个）。"""
    values = (query or {}).get(name) or []
    raw = values[0] if values else None
    if raw is None:
        return default
    return str(raw).lower() not in ("0", "false", "no", "")


def parse_works_filters(query: dict) -> dict:
    """把 /api/works 的查询参数解析成 filters dict（含分页）。

    从 controllers/works.py::get_works 抽出，download 域（targets?mode=filter、
    POST /api/download/filter）复用同一套参数语义，保证「下载当前筛选结果」
    与页面当前列表完全一致。
    """
    q = ((query or {}).get("q", [""])[0] or "").strip() or None
    languages = [v.strip().lower() for v in (query or {}).get("lang", []) if v.strip()]
    dedupe = _bool_param(query, "dedupe", True)
    sort = (query.get("sort", ["recent"])[0] or "recent").lower()
    if sort not in _SORTS:
        sort = "recent"
    return {
        "q": q,
        "languages": languages,
        "dedupe_title": dedupe,
        "sort": sort,
        "page": int_param(query, "page", 1, 1, 100000),
        "per_page": int_param(query, "per_page", 24, 1, 200),
        "uncensored": _bool_param(query, "uncensored"),
        "want_download_only": _bool_param(query, "want"),
        "downloaded_only": _bool_param(query, "downloaded"),
        "site_favorites_only": _bool_param(query, "site_favorites"),
        "batch_id": int_param(query, "batch", 0, 0, 10 ** 13) or None,
    }
