"""抓取任务管理：校验 URL、后台线程、进度状态、逐页入库与详情补全。"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config, covers, db
from .detail import fetch_detail
from .scraper import FetchError, Fetcher, iter_pages


class ScrapeError(RuntimeError):
    """抓取参数/流程错误。"""


LISTING_PREFIXES = ("/group/", "/tag/", "/artist/", "/parody/", "/character/", "/language/")


def normalize_listing_url(raw: str) -> str:
    """校验并规范化列表页 URL（防止 SSRF/误填详情页）。"""
    text = (raw or "").strip()
    if not text:
        raise ScrapeError("请输入 画廊列表页 URL")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ScrapeError(f"URL 无法解析：{exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ScrapeError("只支持 http/https 协议的 URL")
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if path != "/search" and not path.startswith(LISTING_PREFIXES):
        raise ScrapeError("请输入列表页 URL（/group/、/tag/、/artist/、/language/、/search 等），不能是作品详情页 /g/…")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "page"]
    return urlunsplit((parts.scheme, host, path, urlencode(query), ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScrapeService:
    """单任务抓取服务（同一时间只允许一个抓取任务）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = db_path
        # 必须是 RLock：start() 持锁期间会调用 _log()/_progress()，
        # 普通 Lock 会自我死锁，导致任务永远不会启动。
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._state: dict[str, Any] = self._idle_state()
        db.init_db(db_path)

    # ---------- 对外接口 ----------
    def _bool_option(self, value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no")
        return bool(value)

    def start(self, url: str, max_pages: int | str | None = 0,
              delay: float | str | None = None, enrich: bool | None = None,
              covers: bool | None = None, incremental: bool | None = None) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ScrapeError("已有抓取任务正在进行，请等待完成或点击取消")
            clean_url = normalize_listing_url(url)
            try:
                pages = int(max_pages or 0)
            except (TypeError, ValueError):
                pages = 0
            pages = max(0, min(pages, config.MAX_PAGES))
            try:
                delay_value = float(delay) if delay not in (None, "") else config.PAGE_DELAY
            except (TypeError, ValueError):
                delay_value = config.PAGE_DELAY
            delay_value = max(config.MIN_PAGE_DELAY, min(10.0, delay_value))
            enrich_enabled = self._bool_option(enrich, config.ENRICH_DEFAULT)
            covers_on = self._bool_option(covers, config.COVER_DEFAULT)
            incremental_on = self._bool_option(incremental, config.INCREMENTAL)

            site_domain = urlsplit(clean_url).hostname or ""
            # 抓取时把站点地址同步进设置，和门禁里填的域名保持一致
            if site_domain:
                db.set_setting("site_origin",
                               f"{urlsplit(clean_url).scheme}://{site_domain}", self.db_path)
            run_id = db.create_run(clean_url, self.db_path)
            self._cancel = threading.Event()
            self._logs.clear()
            self._state = {
                "status": "running",
                "running": True,
                "url": clean_url,
                "run_id": run_id,
                "started_at": _now(),
                "finished_at": None,
                "pages_total": 0,
                "pages_done": 0,
                "items_seen": 0,
                "items_new": 0,
                "items_dup": 0,
                "details_total": 0,
                "details_done": 0,
                "details_failed": 0,
                "covers_done": 0,
                "covers_failed": 0,
                "enrich": enrich_enabled,
                "covers": covers_on,
                "incremental": incremental_on,
                "error": None,
                "max_pages": pages,
                "delay": delay_value,
            }
            effective = config.effective_proxy()
            proxy_note = f"代理 {effective}" if effective else "直连（未使用代理）"
            self._log(f"开始抓取：{clean_url}（{proxy_note}）")
            if enrich_enabled:
                self._log("已开启详情补全：调用 /api/v2/galleries/{id} 获取原始标题（精确去重）与完整标签")
            else:
                self._log("未开启详情补全：仅使用列表页数据，去重依赖标题指纹")
            if covers_on:
                self._log(f"已开启封面下载：保存到 {config.COVER_DIR}")
            if incremental_on:
                latest = db.latest_run_for_url(clean_url, self.db_path)
                if latest:
                    self._log(f"增量抓取：发现该地址上次成功批次 #{latest['run_id']}，遇到已收录条目即停止")
                else:
                    self._log("增量抓取：首次抓取该地址，将完整抓取")
            else:
                self._log("增量抓取已关闭：将完整抓取整个列表")
            self._thread = threading.Thread(
                target=self._run, args=(run_id, clean_url, pages, delay_value, enrich_enabled, covers_on, site_domain, incremental_on),
                name="gallery-scrape", daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> bool:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
        if alive:
            self._cancel.set()
            self._log("收到取消请求，正在停止…")
        return alive

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            logs = list(self._logs)
            alive = self._thread is not None and self._thread.is_alive()
        state["logs"] = logs
        state["running"] = bool(alive)
        if alive:
            state["status"] = "running"
        return state

    # ---------- 内部实现 ----------
    @staticmethod
    def _idle_state() -> dict:
        return {
            "status": "idle",
            "running": False,
            "url": "",
            "run_id": None,
            "started_at": None,
            "finished_at": None,
            "pages_total": 0,
            "pages_done": 0,
            "items_seen": 0,
            "items_new": 0,
            "items_dup": 0,
            "details_total": 0,
            "details_done": 0,
            "details_failed": 0,
            "covers_done": 0,
            "covers_failed": 0,
            "enrich": None,
            "covers": None,
            "incremental": None,
            "error": None,
        }

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"[{stamp}] {message}")

    def _progress(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _detail_pause(self) -> None:
        remaining = max(0.0, config.DETAIL_DELAY)
        while remaining > 0 and not self._cancel.is_set():
            step = min(0.1, remaining)
            time.sleep(step)
            remaining -= step

    def _cover_pause(self) -> None:
        remaining = max(0.0, config.COVER_DELAY)
        while remaining > 0 and not self._cancel.is_set():
            step = min(0.1, remaining)
            time.sleep(step)
            remaining -= step

    def _download_page_covers(self, fetcher: Fetcher, page, site_domain: str = "",
                              new_ids: list[int] | None = None) -> tuple[int, int]:
        """只为本次新增条目补封面，返回 (成功数, 失败数)。"""
        ids = list(new_ids) if new_ids is not None else [item.gallery_id for item in page.items]
        if not ids:
            return 0, 0
        pending = db.ids_needing_cover(ids, self.db_path)
        if not pending:
            return 0, 0
        urls = db.cover_urls(pending, self.db_path)
        done = failed = 0
        self._log(f"第 {page.page_no} 页需要下载封面 {len(pending)} 张")
        for index, gallery_id in enumerate(pending, start=1):
            if self._cancel.is_set():
                break
            url = urls.get(gallery_id)
            filename = covers.download_cover(
                gallery_id, url or "", cover_dir=config.COVER_DIR, fetcher=fetcher,
                site_domain=site_domain,
            ) if url else None
            if filename:
                db.set_cover(gallery_id, filename, self.db_path)
                done += 1
            else:
                failed += 1
                self._log(f"封面下载失败：#{gallery_id}（下次抓取会重试）")
            if index % 5 == 0 or index == len(pending):
                self._log(f"封面下载进度 {index}/{len(pending)}")
            self._cover_pause()
        return done, failed

    def _enrich_page(self, fetcher: Fetcher, page, site_domain: str = "",
                     log_prefix: str = "", new_ids: list[int] | None = None) -> tuple[int, int, int]:
        """只为本次新增条目补详情，返回 (处理数, 成功数, 失败数)。"""
        ids = list(new_ids) if new_ids is not None else [item.gallery_id for item in page.items]
        if not ids:
            return 0, 0, 0
        pending = db.ids_needing_detail(ids, self.db_path)
        if not pending:
            return 0, 0, 0
        self._log(f"{log_prefix}需要补全详情 {len(pending)} 条")
        done = failed = 0
        for index, gallery_id in enumerate(pending, start=1):
            if self._cancel.is_set():
                break
            detail = fetch_detail(gallery_id, fetcher, site_domain)
            if detail is None:
                failed += 1
                self._log(f"详情补全失败：#{gallery_id}（跳过，下次抓取会重试）")
            else:
                db.apply_details([detail], self.db_path)
                done += 1
                if index % 5 == 0 or index == len(pending):
                    self._log(f"详情补全进度 {index}/{len(pending)}")
            self._detail_pause()
        return len(pending), done, failed

    def _run(self, run_id: int, url: str, max_pages: int, delay: float,
             enrich: bool = True, covers_on: bool = True, site_domain: str = "",
             incremental_on: bool = True) -> None:
        pages_done = 0
        pages_total = 0
        seen = new = dup = 0
        details_total = details_done = details_failed = 0
        covers_done = covers_failed = 0
        status = "done"
        error: str | None = None
        # 增量抓取：只有该地址存在历史成功批次时才启用“遇到已收录即停”
        stop_at_known: int | None = None
        if incremental_on:
            latest = db.latest_run_for_url(url, self.db_path)
            if latest is not None:
                stop_at_known = int(latest["run_id"] or 0)
        try:
            # 不要显式传 proxy：让 Fetcher 走 config.effective_proxy()，
            # 否则会绕过页面的「开启代理」开关
            fetcher = Fetcher()
            for page in iter_pages(
                url,
                max_pages=max_pages,
                delay=delay,
                fetcher=fetcher,
                should_stop=self._cancel.is_set,
                on_log=self._log,
            ):
                if page.page_no == 1 and not page.items:
                    raise ScrapeError("第一页未解析到任何作品：该分组可能没有作品，或页面结构/请求被拦截（请检查代理是否开启）")
                pages_done = page.page_no
                if page.last_page:
                    pages_total = min(page.last_page, max_pages) if max_pages else page.last_page
                result = db.upsert_items(page.items, source_url=page.url,
                                         batch_id=run_id, path=self.db_path)
                seen += result["seen"]
                new += result["new"]
                dup += result["dup"]
                self._log(
                    f"第 {page.page_no} 页入库：新增 {result['new']}，"
                    f"已存在跳过 {result['dup']}（按规则不更新原始记录）"
                )
                self._progress(
                    pages_done=pages_done, pages_total=pages_total,
                    items_seen=seen, items_new=new, items_dup=dup,
                )
                if enrich:
                    batch_total, batch_done, batch_failed = self._enrich_page(
                        fetcher, page, site_domain=site_domain,
                        log_prefix=f"第 {page.page_no} 页",
                        new_ids=result.get("new_ids"),
                    )
                    details_total += batch_total
                    details_done += batch_done
                    details_failed += batch_failed
                    self._progress(
                        details_total=details_total,
                        details_done=details_done,
                        details_failed=details_failed,
                    )
                if covers_on:
                    batch_done, batch_failed = self._download_page_covers(
                        fetcher, page, site_domain=site_domain,
                        new_ids=result.get("new_ids"))
                    covers_done += batch_done
                    covers_failed += batch_failed
                    self._progress(covers_done=covers_done, covers_failed=covers_failed)
                # 增量抓取：同一列表地址已有成功批次时，遇到“本页出现已收录条目”即停止。
                # 前提是上游列表按时间倒序返回，遇到旧条目后后续只会更旧，无需继续翻页。
                if (incremental_on and stop_at_known is not None and page.page_no >= 1
                        and result["dup"] > 0):
                    self._log(
                        f"第 {page.page_no} 页已出现已收录条目（重复 {result['dup']}，"
                        f"新增 {result['new']}），增量抓取到此停止，后续旧数据不再请求"
                    )
                    break
            if self._cancel.is_set():
                status = "cancelled"
                self._log("抓取已取消（已抓取的数据已保存）")
            else:
                notes = []
                if enrich:
                    notes.append(f"详情补全 {details_done}/{details_total}（失败 {details_failed}）")
                if covers_on:
                    notes.append(f"封面 {covers_done} 张（失败 {covers_failed}）")
                note_text = ("，" + "，".join(notes)) if notes else ""
                self._log(f"完成：抓取 {pages_done} 页，条目 {seen}（新增 {new}，重复 {dup}）{note_text}")
        except (FetchError, ScrapeError) as exc:
            status = "error"
            error = str(exc)
            self._log(f"失败：{error}")
        except Exception as exc:  # 防御性兜底
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"失败：{error}")
            traceback.print_exc()
        finally:
            self._progress(
                status=status,
                running=False,
                finished_at=_now(),
                pages_done=pages_done,
                pages_total=pages_total,
                items_seen=seen,
                items_new=new,
                items_dup=dup,
                details_total=details_total,
                details_done=details_done,
                details_failed=details_failed,
                covers_done=covers_done,
                covers_failed=covers_failed,
                error=error,
            )
            try:
                db.finish_run(
                    run_id, status=status, pages_total=pages_total, pages_done=pages_done,
                    items_seen=seen, items_new=new, items_dup=dup, error=error, path=self.db_path,
                )
                # 爬取结果为 0（没有新增任何条目）时，不保留这个批次号
                if status == "done" and new == 0:
                    db.delete_run(run_id, self.db_path)
                    self._progress(run_id=None)
                    self._log("本次抓取没有新增条目，批次号已删除（避免空批次占用）")
                elif status == "done":
                    # 同一 URL 只保留最新一次批次：旧批次并入本次批次
                    merged = db.merge_runs_for_url(url, run_id, self.db_path)
                    if merged:
                        self._log(f"同一地址只保留最新批次：已清理并合并 {merged} 个旧批次")
            except Exception:
                traceback.print_exc()