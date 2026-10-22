"""controller：抓取域（启动/取消/状态）。业务在 service.ScrapeService。"""
from __future__ import annotations

from app import scrape_service as service_module
from app.scrape_service import ScrapeError


def post_scrape(h) -> None:
    payload = h._read_json()
    if payload is None:
        return
    user = h._current_user()
    # 抓取在两种模式下都可用：本地模式写用户 SQLite，云模式（CloudSink）直接写云端。
    # 出网走 config.effective_proxy()（云 VPS 可直连，本地走 10808 代理）。
    service = h._scrape_service(user)
    try:
        state = service.start(
            payload.get("url", ""),
            max_pages=payload.get("max_pages", 0),
            delay=payload.get("delay"),
            enrich=payload.get("enrich"),
            covers=payload.get("covers"),
            incremental=payload.get("incremental"),
        )
    except ScrapeError as exc:
        busy = "正在进行" in str(exc)
        h._error(str(exc), 409 if busy else 400)
        return
    h._json({"ok": True, "scrape": state})


def post_scrape_cancel(h) -> None:
    user = h._current_user()
    cancelled = h._scrape_service(user).cancel()
    h._json({"ok": True, "cancelled": cancelled,
             **h._scrape_service(user).snapshot()})


def get_scrape_status(h) -> None:
    user = h._current_user()
    h._json({"ok": True, **h._scrape_service(user).snapshot()})
