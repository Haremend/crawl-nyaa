#!/usr/bin/env python
"""claw-gallery 启动入口。

用法：
    python run.py                # 启动 Web 服务（默认 http://127.0.0.1:8787）
    python run.py --open         # 启动后自动打开浏览器
    python run.py --seed-demo    # 写入离线演示数据，先预览前端效果
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config, db  # noqa: E402
from app.server import run_server  # noqa: E402


def ensure_user_dbs() -> None:
    """启动时统一检查/初始化用户数据：为每个已注册用户库确保全表。

    用户库平时在注册/登录云回退时建；历史上云模式跳过了这一步（业务数据
    直连 Supabase、不落本地），于是 download_jobs/settings/scrape_runs/galleries
    缺表——下载队列（download_jobs + 兼容回退读 settings.site_origin）与
    save_credentials 写用户库 settings 会踩到「no such table」。在启动时把
    所有用户库幂等初始化一次（CREATE TABLE IF NOT EXISTS + 迁移），既给
    已存在的残缺库补表，也给云模式下从未建库的用户建好全表，让两种存储
    模式下「读侧/下载队列始终可用、写侧不依赖用户库」都成立。
    """
    _ensure_unlogin_user()
    user_paths = list(db.iter_user_db_paths(config.DB_PATH))
    for path in user_paths:
        db.init_db(path)
    print(f"[启动] 数据检查   ：已为 {len(user_paths)} 个用户库确保全表")


def _ensure_unlogin_user() -> None:
    """兜底注册内置匿名账号 unlogin_user（不存在时）。

    匿名业务数据挂在此账号的用户库 data/users/<unlogin_user>.sqlite3；
    密码为随机占位值，任何人都输不出来、恒不能登录。已存在则跳过。
    """
    name = config.UNLOGIN_USER
    if db.get_user(name, config.DB_PATH):
        return
    from secrets import token_hex
    # 随机密码经 register_user 的 PBKDF2 哈希落库，无人知晓、恒不能登录
    db.register_user(name, token_hex(16), path=config.DB_PATH)
    db.init_db(db.user_db_path(name, base_dir=Path(config.DB_PATH).parent))
    print(f"[启动] 数据检查   ：已兜底创建匿名账号 {name}（随机占位密码，不可登录）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="画廊作品列表抓取 + 本地浏览（SQLite）")
    parser.add_argument("--host", default=config.HOST, help=f"监听地址（默认 {config.HOST}）")
    parser.add_argument("--port", type=int, default=config.PORT, help=f"监听端口（默认 {config.PORT}）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--seed-demo", action="store_true", help="写入 samples/listing_demo.html 中的演示数据")
    args = parser.parse_args(argv)

    print(f"[启动] 项目目录   ：{Path(__file__).resolve().parent}")
    print(f"[启动] 数据目录   ：{config.DATA_DIR}")
    print(f"[启动] 默认账号库 ：{config.DB_PATH}")
    print(f"[启动] 用户库目录 ：{config.USER_DATA_DIR}")
    print(f"[启动] 封面目录   ：{config.COVER_DIR}")
    print(f"[启动] 下载目录   ：{config.DOWNLOADS_DIR}")
    print(f"[启动] 存储模式   ：{config.STORAGE_BACKEND}"
          f"（云模式未配置 Supabase 时自动回退 local）")

    db.init_db()
    ensure_user_dbs()
    if args.seed_demo:
        from app.demo import seed_demo
        inserted = seed_demo()
        print(f"已写入演示数据：{inserted} 条（仅用于离线预览前端）")

    run_server(args.host, args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"\n[启动失败] {type(exc).__name__}: {exc}")
        print(f"[诊断] 数据库文件：{config.DB_PATH}")
        print(f"[诊断] 数据目录  ：{config.DATA_DIR}（父目录 {config.DATA_DIR.parent}）")
        print(f"[诊断] 当前工作目录：{Path.cwd()}")
        print(f"[诊断] 数据目录父级可写：{os.access(str(config.DATA_DIR.parent), os.W_OK)}")
        raise SystemExit(1)
