"""抓取任务管理：校验 URL、后台线程、进度状态、逐页入库与详情补全。

存储 sink 抽象：
- 本地模式：`SQLiteSink`（现状，写 `data/users/<用户>.sqlite3` / 默认库）。
- 云模式（默认）：`CloudSink`（写 Supabase，不落本地 SQLite）。
`ScrapeService` 持有一个 sink，抓取流程（翻页 / 增量 / 详情 / 封面 / 批次合并）
完全不变，只把「入库 / 批次记录 / 站点设置」换成 sink 调用。
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config, covers, db, supabase_client
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


# ============================================================
# 存储 sink：抓取流程的「写库」抽象
# ============================================================

class ScrapeSink:
    """抓取流程所需的持久化接口。本地=SQLite，云端=Supabase。"""

    # 设置站点 origin（抓取时把列表页域名写回设置）
    def set_site_origin(self, origin: str) -> None:
        raise NotImplementedError

    # 批次记录
    def create_run(self, url: str) -> int:
        raise NotImplementedError

    def finish_run(self, run_id: int, *, status: str, pages_total: int = 0,
                   pages_done: int = 0, items_seen: int = 0, items_new: int = 0,
                   items_dup: int = 0, error: str | None = None) -> None:
        raise NotImplementedError

    def latest_run_for_url(self, url: str) -> dict | None:
        raise NotImplementedError

    def delete_run(self, run_id: int) -> bool:
        raise NotImplementedError

    def merge_runs_for_url(self, url: str, keep_run_id: int) -> int:
        raise NotImplementedError

    # 作品入库（幂等 upsert，返回 new_ids）
    def upsert_items(self, items, *, source_url: str, batch_id: int | None) -> dict:
        raise NotImplementedError

    # 详情补全
    def ids_needing_detail(self, ids) -> list[int]:
        raise NotImplementedError

    def apply_details(self, details) -> int:
        raise NotImplementedError

    # 封面
    def ids_needing_cover(self, ids) -> list[int]:
        raise NotImplementedError

    def cover_urls(self, ids) -> dict[int, str]:
        raise NotImplementedError

    def set_cover(self, gallery_id: int, filename: str) -> bool:
        raise NotImplementedError


class SQLiteSink(ScrapeSink):
    """本地模式：直接复用现有 db.* 调用（与改造前行为一致）。"""

    def __init__(self, db_path):
        self.db_path = db_path

    def set_site_origin(self, origin):
        db.set_setting("site_origin", origin, self.db_path)

    def create_run(self, url):
        return db.create_run(url, self.db_path)

    def finish_run(self, run_id, **kw):
        db.finish_run(run_id, path=self.db_path, **kw)

    def latest_run_for_url(self, url):
        return db.latest_run_for_url(url, self.db_path)

    def delete_run(self, run_id):
        return db.delete_run(run_id, self.db_path)

    def merge_runs_for_url(self, url, keep_run_id):
        return db.merge_runs_for_url(url, keep_run_id, self.db_path)

    def upsert_items(self, items, *, source_url, batch_id):
        return db.upsert_items(items, source_url=source_url, batch_id=batch_id,
                               path=self.db_path)

    def ids_needing_detail(self, ids):
        return db.ids_needing_detail(ids, self.db_path)

    def apply_details(self, details):
        return db.apply_details(details, self.db_path)

    def ids_needing_cover(self, ids):
        return db.ids_needing_cover(ids, self.db_path)

    def cover_urls(self, ids):
        return db.cover_urls(ids, self.db_path)

    def set_cover(self, gallery_id, filename):
        return db.set_cover(gallery_id, filename, self.db_path)


class CloudSink(ScrapeSink):
    """云模式：抓取数据直接写 Supabase（不落本地 SQLite）。

    - 作品行：内存做幂等去重（拉一次该用户全部 gallery_id），新 ID 进 pending，
      详情/封面更新也累积到 pending，统一 push_user 落云。
    - 批次记录：create_run/finish_run/delete_run/merge 走云端 SQL。
    - 站点 origin：写云端账号行 claw_users.site_origin。
    """

    def __init__(self, username: str):
        self.username = username
        # 抓取会话内累积待推送的作品行（gallery_id -> row dict）
        self._pending: dict[int, dict] = {}
        self._existing_ids: set[int] | None = None   # 该用户云端已有 gallery_id
        self._created_runs: set[int] = set()

    # ---------- 辅助 ----------
    def _existing(self) -> set[int]:
        if self._existing_ids is None:
            rows = supabase_client.cloud_fetch_gallery_rows(self.username)
            self._existing_ids = {int(r.get("gallery_id") or 0) for r in rows if r.get("gallery_id")}
        return self._existing_ids

    @staticmethod
    def _row_from_item(item, source_url: str, now: str, batch_id: int | None) -> dict:
        """把 GalleryItem / 收藏行 / 详情行转成云端 galleries 行（列与本地 galleries 对齐）。"""
        raw = dict(item.as_row()) if hasattr(item, "as_row") else dict(item)
        gid = int(raw.get("gallery_id") or 0)
        langs = [str(x).lower() for x in _as_list(raw.get("languages")) if str(x).strip()]
        return {
            "gallery_id": gid,
            "media_id": raw.get("media_id"),
            "title": str(raw.get("title") or f"#{gid}"),
            "title_pretty": str(raw.get("title_pretty") or ""),
            "title_original": str(raw.get("title_original") or ""),
            "title_key": str(raw.get("title_key") or f"id:{gid}"),
            "original_key": str(raw.get("original_key") or ""),
            "url": str(raw.get("url") or f"/g/{gid}/"),
            "thumbnail": str(raw.get("thumbnail") or ""),
            "languages": json_dumps_list(langs),
            "primary_language": langs[0] if langs else "unknown",
            "tags": json_dumps_list(raw.get("tags")),
            "artists": json_dumps_list(raw.get("artists")),
            "groups": json_dumps_list(raw.get("groups")),
            "parodies": json_dumps_list(raw.get("parodies")),
            "characters": json_dumps_list(raw.get("characters")),
            "categories": json_dumps_list(raw.get("categories")),
            "cover_file": str(raw.get("cover_file") or ""),
            "cover_fetched_at": str(raw.get("cover_fetched_at") or ""),
            "scanlator": str(raw.get("scanlator") or ""),
            "uncensored": 1 if (raw.get("uncensored") or 0) else 0,
            "want_download": 1 if (raw.get("want_download") or 0) else 0,
            "downloaded": 0,
            "downloaded_at": "",
            "hidden": 0,
            "site_favorite": 1 if (raw.get("site_favorite") or 0) else 0,
            "from_scrape": 1 if raw.get("from_scrape", 1) else 0,
            "batch_id": int(batch_id or 0),
            "num_pages": raw.get("num_pages"),
            "num_favorites": raw.get("num_favorites"),
            "upload_date": str(raw.get("upload_date") or ""),
            "source_url": source_url or "",
            "detail_fetched_at": str(raw.get("detail_fetched_at") or ""),
            "first_seen": now,
            "last_seen": now,
        }

    # ---------- 作品入库 ----------
    def upsert_items(self, items, *, source_url, batch_id):
        now = db.utc_now()
        existing = self._existing()
        new_ids: list[int] = []
        seen = dup = 0
        for item in items:
            row = self._row_from_item(item, source_url, now, batch_id)
            gid = int(row.get("gallery_id") or 0)
            if gid <= 0:
                continue
            seen += 1
            if gid in existing:
                dup += 1
                # 已收录：只刷新 last_seen（云端 upsert 的较新者胜守卫保证不覆盖旧内容）
                old = self._pending.get(gid)
                if old:
                    old["last_seen"] = now
                else:
                    self._pending[gid] = {"gallery_id": gid, "last_seen": now}
                continue
            self._pending[gid] = row
            new_ids.append(gid)
        if new_ids:
            # 只 push 本页新增的行（已有行已在云端，靠 upsert 较新者胜守卫刷新 last_seen）
            supabase_client.push_user(self.username,
                                      [self._pending[g] for g in new_ids], [])
            for gid in new_ids:
                existing.add(gid)
        return {"seen": seen, "new": len(new_ids), "dup": dup,
                "unchanged": 0, "skipped": dup, "updated": 0, "new_ids": new_ids}

    def ids_needing_detail(self, ids):
        # 云模式：详情补全结果会累积进 pending；「需要详情」= 还没有 detail_fetched_at
        existing = self._existing()
        need = [int(g) for g in ids if int(g or 0) > 0 and int(g) in self._pending
                and not self._pending[int(g)].get("detail_fetched_at")]
        return need

    def apply_details(self, details):
        """把详情补全结果合并进 pending（detail_fetched_at / 原始标题 / 标签等），统一 push 落云。"""
        now = db.utc_now()
        count = 0
        for d in details:
            # GalleryDetail 有 as_row()（列与 galleries 一致）；合并到已有 pending 行
            raw = dict(d.as_row()) if hasattr(d, "as_row") else dict(d)
            gid = int(raw.get("gallery_id") or 0)
            if not gid:
                continue
            base = self._pending.get(gid) or {"gallery_id": gid, "last_seen": now}
            base["detail_fetched_at"] = now
            for col in ("media_id", "title_pretty", "title_original", "original_key", "thumbnail",
                        "languages", "primary_language", "tags", "artists", "groups", "parodies",
                        "characters", "categories", "scanlator", "uncensored", "num_pages",
                        "num_favorites", "upload_date"):
                value = raw.get(col)
                if value in (None, ""):
                    continue
                if col in ("languages", "tags", "artists", "groups", "parodies",
                           "characters", "categories"):
                    base[col] = json_dumps_list(value)
                else:
                    base[col] = value
            self._pending[gid] = base
            count += 1
        if count:
            supabase_client.push_user(self.username, list(self._pending.values()), [])
        return count

    def ids_needing_cover(self, ids):
        need = [int(g) for g in ids if int(g or 0) > 0 and g in self._pending
                and not self._pending[int(g)].get("cover_file")]
        return need

    def cover_urls(self, ids):
        out: dict[int, str] = {}
        for g in ids:
            gid = int(g or 0)
            row = self._pending.get(gid)
            if row and row.get("thumbnail"):
                out[gid] = str(row["thumbnail"])
        return out

    def set_cover(self, gallery_id, filename):
        gid = int(gallery_id or 0)
        if gid not in self._pending:
            return False
        self._pending[gid]["cover_file"] = filename
        self._pending[gid]["cover_fetched_at"] = db.utc_now()
        supabase_client.push_user(self.username, [self._pending[gid]], [])
        return True

    # ---------- 批次记录 ----------
    def create_run(self, url):
        run_id = int(time.time() * 1000)
        supabase_client.run_sql(
            f'insert into public.{supabase_client._q(supabase_client.RUNS_TABLE)} '
            f'("username","url","run_id","status","started_at","pages_total","pages_done",'
            f'"items_seen","items_new","items_dup") '
            f'values ({supabase_client._literal(self.username)}, {supabase_client._literal(url)}, '
            f'{run_id}, \'running\', {supabase_client._literal(db.utc_now())}, 0,0,0,0,0) '
            f'on conflict ("username","url") do update set "run_id"=excluded."run_id",'
            f'"status"=\'running\', "started_at"=excluded."started_at"',
        )
        self._created_runs.add(run_id)
        return run_id

    def finish_run(self, run_id, **kw):
        sql = (
            f'update public.{supabase_client._q(supabase_client.RUNS_TABLE)} set '
            f'"status"={supabase_client._literal(str(kw.get("status") or ""))}, '
            f'"finished_at"={supabase_client._literal(db.utc_now())}, '
            f'"pages_total"={int(kw.get("pages_total") or 0)}, '
            f'"pages_done"={int(kw.get("pages_done") or 0)}, '
            f'"items_seen"={int(kw.get("items_seen") or 0)}, '
            f'"items_new"={int(kw.get("items_new") or 0)}, '
            f'"items_dup"={int(kw.get("items_dup") or 0)}, '
            f'"error"={supabase_client._literal(kw.get("error"))} '
            f'where "username"={supabase_client._literal(self.username)} and "run_id"={run_id}'
        )
        supabase_client.run_sql(sql)

    def latest_run_for_url(self, url):
        rows = supabase_client.run_sql(
            f'select "run_id","url","status","started_at","finished_at" from '
            f'public.{supabase_client._q(supabase_client.RUNS_TABLE)} '
            f'where "username"={supabase_client._literal(self.username)} '
            f'and "url"={supabase_client._literal(url)} and "status"=\'done\' '
            f'order by "run_id" desc limit 1', read_only=True)
        return dict(rows[0]) if rows else None

    def delete_run(self, run_id):
        supabase_client.run_sql(
            f'delete from public.{supabase_client._q(supabase_client.RUNS_TABLE)} '
            f'where "username"={supabase_client._literal(self.username)} and "run_id"={run_id}')
        return True

    def merge_runs_for_url(self, url, keep_run_id):
        old = supabase_client.run_sql(
            f'select "run_id" from public.{supabase_client._q(supabase_client.RUNS_TABLE)} '
            f'where "username"={supabase_client._literal(self.username)} '
            f'and "url"={supabase_client._literal(url)} and "run_id" <> {keep_run_id}',
            read_only=True)
        old_ids = [int(r["run_id"]) for r in old if r.get("run_id")]
        if not old_ids:
            return 0
        marks = ", ".join(str(i) for i in old_ids)
        supabase_client.run_sql(
            f'update public.{supabase_client._q(supabase_client.GALLERIES_TABLE)} '
            f'set "batch_id"={keep_run_id} where "username"={supabase_client._literal(self.username)} '
            f'and "batch_id" in ({marks})')
        supabase_client.run_sql(
            f'delete from public.{supabase_client._q(supabase_client.RUNS_TABLE)} '
            f'where "username"={supabase_client._literal(self.username)} and "run_id" in ({marks})')
        return len(old_ids)

    # ---------- 站点设置 ----------
    def set_site_origin(self, origin):
        acc = supabase_client.fetch_account(self.username) or {}
        supabase_client.push_user_credentials(
            self.username,
            api_key=str(acc.get("api_key") or ""),
            site_origin=origin,
            password_hash=str(acc.get("password_hash") or ""),
            salt=str(acc.get("salt") or ""),
            created_at=str(acc.get("created_at") or ""),
        )


def json_dumps_list(value) -> str:
    import json as _json
    if isinstance(value, (list, tuple)):
        items = [str(x) for x in value if str(x).strip()]
    else:
        items = []
    return _json.dumps(items, ensure_ascii=False)


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


class ScrapeService:
    """单任务抓取服务（同一时间只允许一个抓取任务）。"""

    def __init__(self, db_path: str | Path | None = None, sink: ScrapeSink | None = None) -> None:
        self.db_path = db_path
        self.sink: ScrapeSink = sink or SQLiteSink(db_path)
        # 必须是 RLock：start() 持锁期间会调用 _log()/_progress()，
        # 普通 Lock 会自我死锁，导致任务永远不会启动。
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._state: dict[str, Any] = self._idle_state()
        # 本地模式才需要本地库；云模式 sink 不依赖 SQLite
        if not isinstance(self.sink, CloudSink):
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
            # 抓取时把站点地址同步进设置（本地写 settings，云写 claw_users）
            if site_domain:
                self.sink.set_site_origin(f"{urlsplit(clean_url).scheme}://{site_domain}")
            run_id = self.sink.create_run(clean_url)
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
                latest = self.sink.latest_run_for_url(clean_url)
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
        pending = self.sink.ids_needing_cover(ids)
        if not pending:
            return 0, 0
        urls = self.sink.cover_urls(pending)
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
                self.sink.set_cover(gallery_id, filename)
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
        pending = self.sink.ids_needing_detail(ids)
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
                self.sink.apply_details([detail])
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
            latest = self.sink.latest_run_for_url(url)
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
                result = self.sink.upsert_items(page.items, source_url=page.url, batch_id=run_id)
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
                note_text = ("，" + "。".join(notes)) if notes else ""
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
                self.sink.finish_run(
                    run_id, status=status, pages_total=pages_total, pages_done=pages_done,
                    items_seen=seen, items_new=new, items_dup=dup, error=error,
                )
                # 爬取结果为 0（没有新增任何条目）时，不保留这个批次号
                if status == "done" and new == 0:
                    self.sink.delete_run(run_id)
                    self._progress(run_id=None)
                    self._log("本次抓取没有新增条目，批次号已删除（避免空批次占用）")
                elif status == "done":
                    # 同一 URL 只保留最新一次批次：旧批次并入本次批次
                    merged = self.sink.merge_runs_for_url(url, run_id)
                    if merged:
                        self._log(f"同一地址只保留最新批次：已清理并合并 {merged} 个旧批次")
            except Exception:
                traceback.print_exc()
