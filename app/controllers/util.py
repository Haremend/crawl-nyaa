"""controller 层共用小工具（不碰 Handler 状态）。"""
from __future__ import annotations


def int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    """取 query 里的 int 参数并夹到 [low, high]；缺省/非法用 default。"""
    values = (query or {}).get(name) or []
    raw = values[0] if values else None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))
