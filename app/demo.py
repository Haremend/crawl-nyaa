"""离线演示数据：把 samples/listing_demo.html 解析入库，便于无网络预览前端。"""
from __future__ import annotations

from pathlib import Path

from . import config, db
from .parser import parse_listing

DEMO_SOURCE = "demo://samples/listing_demo.html"


def seed_demo(db_path: str | Path | None = None) -> int:
    sample = config.SAMPLES_DIR / "listing_demo.html"
    if not sample.is_file():
        raise FileNotFoundError(f"缺少演示数据文件：{sample}")
    page = parse_listing(sample.read_text(encoding="utf-8"), config.DEFAULT_URL)
    result = db.upsert_items(page.items, source_url=DEMO_SOURCE, path=db_path)
    return result["seen"]