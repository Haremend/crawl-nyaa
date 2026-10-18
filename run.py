#!/usr/bin/env python
"""claw-gallery 启动入口。

用法：
    python run.py                # 启动 Web 服务（默认 http://127.0.0.1:8787）
    python run.py --open         # 启动后自动打开浏览器
    python run.py --seed-demo    # 写入离线演示数据，先预览前端效果
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config, db  # noqa: E402
from app.server import run_server  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="画廊作品列表抓取 + 本地浏览（SQLite）")
    parser.add_argument("--host", default=config.HOST, help=f"监听地址（默认 {config.HOST}）")
    parser.add_argument("--port", type=int, default=config.PORT, help=f"监听端口（默认 {config.PORT}）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--seed-demo", action="store_true", help="写入 samples/listing_demo.html 中的演示数据")
    args = parser.parse_args(argv)

    db.init_db()
    if args.seed_demo:
        from app.demo import seed_demo
        inserted = seed_demo()
        print(f"已写入演示数据：{inserted} 条（仅用于离线预览前端）")

    run_server(args.host, args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())