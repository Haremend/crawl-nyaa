"""脱敏的云端请求日志工具（service 层共用，避免反向依赖 app.server）。

与 app/server.py 的 _cloud_log 同构：打到 stderr（不进 JSON 响应/前端），
只记脱敏字段（username / 方向 / 行数 / HTTP 状态），**绝不**打账号 dict 或
请求体——里面带 API Key（AGENTS.md §1.2）。
"""
from __future__ import annotations

import datetime
import sys


def cloud_log(*parts) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] [cloud] " + " ".join(str(p) for p in parts), file=sys.stderr)
