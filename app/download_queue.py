"""服务端下载队列。

设计要点：
- 队列存在 SQLite（download_jobs 表），因此任何浏览器标签页看到的都是同一份队列
- 固定数量的 worker 并发消费（默认 5，可用 CLAW_DOWNLOAD_CONCURRENCY 调整）
- 每个任务记录 done_bytes / total_bytes，前端可显示实时进度
- 同一作品 + 格式重复入队会自动合并，不会排队下载两次
- API Key 只保存在内存里跟着任务走，不写数据库；服务重启后未完成的任务会因缺少 Key 失败
"""
from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from pathlib import Path

from . import config, db
from .filenames import download_filename
from .scraper import FetchError, Fetcher
from .upstream import UpstreamClient, UpstreamError

CHUNK_SIZE = 65536
PROGRESS_INTERVAL = 0.4


class _Cancelled(Exception):
    """任务被取消。"""


class DownloadQueue:
    """带并发与进度上报的服务端下载队列。"""

    def __init__(self, db_path: str | Path | None = None, workers: int | None = None) -> None:
        self.db_path = db_path
        self.workers = max(1, int(workers or config.DOWNLOAD_CONCURRENCY))
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._keys: dict[int, str] = {}
        self._events: dict[int, threading.Event] = {}
        db.init_db(db_path)
        db.reset_stale_jobs(self.db_path)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            for index in range(self.workers):
                thread = threading.Thread(target=self._worker, args=(index,),
                                          name=f"dl-worker-{index}", daemon=True)
                thread.start()
                self._threads.append(thread)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
        for thread in list(self._threads):
            thread.join(timeout=2)
        with self._lock:
            self._threads.clear()
            self._started = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    # ---------- 入队 ----------
    def enqueue(self, gallery_id: int, fmt: str = "zip", *, api_key: str = "",
                source: str = "manual", batch_id: int | None = None,
                title: str = "", username: str = "", origin: str = "") -> dict:
        fmt = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
        name = (username or "").strip()
        org = (origin or "").strip()
        # 标题用于生成合规文件名；若调用方没传，按用户库取（没用户就退回默认库）
        user_db = db.user_db_path(name) if name else self.db_path
        resolved_title = title or db.get_title(gallery_id, user_db)
        job_id = db.create_job(gallery_id, title=resolved_title, fmt=fmt,
                               source=source, batch_id=batch_id, username=name,
                               origin=org, path=self.db_path)
        with self._lock:
            if api_key:
                self._keys[job_id] = api_key
            self._events.setdefault(job_id, threading.Event())
        self.start()
        return db.get_job(job_id, self.db_path) or {}

    def enqueue_many(self, items, fmt: str = "zip", *, api_key: str = "",
                     source: str = "batch", batch_id: int | None = None,
                     username: str = "", origin: str = "") -> dict:
        queued = 0
        for item in items:
            gid = int(item.get("gallery_id") or 0)
            if gid <= 0:
                continue
            self.enqueue(gid, fmt, api_key=api_key, source=source,
                         batch_id=batch_id, title=str(item.get("title") or ""),
                         username=username, origin=origin)
            queued += 1
        self.start()
        return {"queued": queued, "total": len(items)}

    # ---------- 取消 ----------
    def cancel(self, job_id: int) -> dict:
        with self._lock:
            event = self._events.get(int(job_id))
        if event is not None:
            event.set()
            return {"cancelled": True, "running": True}
        cancelled = db.cancel_job(job_id, self.db_path)
        return {"cancelled": cancelled, "running": False}

    def cancel_all(self) -> dict:
        with self._lock:
            events = list(self._events.values())
        for event in events:
            event.set()
        count = db.cancel_queued_jobs(self.db_path)
        return {"cancelled_queued": count, "signalled_running": len(events)}

    def clear_failed_jobs(self, username: str = "") -> int:
        """删除当前用户（或全部）的 failed/cancelled 任务。"""
        return db.clear_failed_jobs(self.db_path, username=username)

    # ---------- 状态 ----------
    def snapshot(self, limit: int = 100) -> dict:
        jobs = db.list_jobs(self.db_path, limit=limit)
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for job in jobs:
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {"workers": self.workers, "started": self.running,
                "counts": counts, "jobs": jobs}
    # ---------- worker ----------
    def _worker(self, index: int) -> None:
        while not self._stop.is_set():
            # 注意：claim_next_job 必须在 try 内——若 download_jobs 表缺失或 DB
            # 未初始化，SELECT 会抛 OperationalError；放在 try 外会让 worker
            # 线程直接退出死亡，5 个全死则整队静默停摆。这里捕获后自愈建表，
            # 退避重试，保证 worker 永久存活。
            try:
                job = db.claim_next_job(self.db_path)
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    try:
                        db.init_db(self.db_path)
                    except Exception:
                        traceback.print_exc()
                else:
                    traceback.print_exc()
                time.sleep(1.0)
                continue
            except Exception:
                traceback.print_exc()
                time.sleep(1.0)
                continue
            if job is None:
                time.sleep(0.4)
                continue
            try:
                self._process(job)
            except Exception as exc:
                db.update_job(int(job["job_id"]), path=self.db_path, status="failed",
                              error=f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
            finally:
                with self._lock:
                    self._events.pop(int(job["job_id"]), None)
                    self._keys.pop(int(job["job_id"]), None)

    def _process(self, job: dict) -> None:
        job_id = int(job["job_id"])
        gallery_id = int(job["gallery_id"])
        fmt = str(job.get("format") or "zip")
        title = str(job.get("title") or "")
        username = str(job.get("username") or "").strip()
        # 任务绑定的站点地址：入队时已经按当前用户解析好写进任务行，
        # 这样 worker 不再依赖 download_jobs 库里的站点设置（那是实例级共享库）
        origin = str(job.get("origin") or "").strip()
        with self._lock:
            event = self._events.setdefault(job_id, threading.Event())
            api_key = self._keys.get(job_id, "")

        # 完成后的「已下载」标记要落在该用户的库；文件全局共享，
        # 其它用户以后触发同一作品时也会命中并标记自己的库。
        user_db = db.user_db_path(username) if username else self.db_path

        if event.is_set():
            db.update_job(job_id, path=self.db_path, status="cancelled",
                          finished_at=db.utc_now())
            return
        if not api_key:
            db.update_job(job_id, path=self.db_path, status="failed",
                          error="缺少 API Key（服务重启后请重新加入队列）",
                          finished_at=db.utc_now())
            return

        if not origin:
            # 兼容老任务行（没有 origin）：回退到用户库的站点设置
            origin = db.get_site_origin(user_db)
        if not origin:
            db.update_job(job_id, path=self.db_path, status="failed",
                          error="未知站点地址：请先在设置里填写站点域名",
                          finished_at=db.utc_now())
            return

        download_dir = Path(config.DOWNLOADS_DIR)
        download_dir.mkdir(parents=True, exist_ok=True)
        target = download_dir / download_filename(gallery_id, title, fmt)

        existing = self._existing_file(download_dir, gallery_id, fmt)
        if existing is not None:
            size = existing.stat().st_size
            db.update_job(job_id, path=self.db_path, status="done", file_path=str(existing),
                          total_bytes=size, done_bytes=size, finished_at=db.utc_now())
            # 文件已存在：直接命中，按当前用户标记已下载（不替其他用户写状态）
            db.set_downloaded([gallery_id], True, user_db)
            return

        try:
            info = UpstreamClient(origin, api_key).get_download_url(gallery_id, fmt)
        except UpstreamError as exc:
            db.update_job(job_id, path=self.db_path, status="failed", error=str(exc),
                          finished_at=db.utc_now())
            return
        url = str(info.get("url") or "")
        if not url:
            db.update_job(job_id, path=self.db_path, status="failed",
                          error="上游未返回下载地址", finished_at=db.utc_now())
            return

        fetcher = Fetcher()
        try:
            response = fetcher.open_stream(url, referer=f"{origin}/g/{gallery_id}/")
        except FetchError as exc:
            db.update_job(job_id, path=self.db_path, status="failed", error=str(exc),
                          finished_at=db.utc_now())
            return

        temp = target.with_name(target.name + ".part")
        done_bytes = 0
        total = 0
        try:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            db.update_job(job_id, path=self.db_path, total_bytes=total, done_bytes=0)

            last_report = 0.0
            with open(temp, "wb") as handle:
                while True:
                    if event.is_set():
                        raise _Cancelled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done_bytes += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= PROGRESS_INTERVAL:
                        last_report = now
                        db.update_job(job_id, path=self.db_path, done_bytes=done_bytes)
            if done_bytes <= 0:
                raise FetchError("上游返回空文件")
            temp.replace(target)
        except _Cancelled:
            self._cleanup(temp)
            db.update_job(job_id, path=self.db_path, status="cancelled",
                          done_bytes=done_bytes, finished_at=db.utc_now())
            return
        except (OSError, FetchError) as exc:
            self._cleanup(temp)
            db.update_job(job_id, path=self.db_path, status="failed", error=str(exc),
                          done_bytes=done_bytes, finished_at=db.utc_now())
            return
        finally:
            try:
                response.close()
            except Exception:
                pass

        db.update_job(job_id, path=self.db_path, status="done", file_path=str(target),
                      total_bytes=total or done_bytes, done_bytes=done_bytes,
                      finished_at=db.utc_now())
        # 文件在服务器上只存一份：当前用户标记自己的库即可，
        # 其它用户以后触发同一作品时会命中磁盘文件并标记自己的库。
        db.set_downloaded([gallery_id], True, user_db)
        manifest_batch_id = job.get("batch_id")
        if str(job.get("source") or "") == "batch" and manifest_batch_id:
            self._write_batch_manifest(int(manifest_batch_id), download_dir, user_db)

    def _write_batch_manifest(self, batch_id: int, directory: Path, user_db) -> None:
        """在下载目录里写清单（JSON + CSV），记录来源与作品列表。

        批次文件与作品文件统一放下载根目录，清单用 batch_{id} 前缀避免多批次互相覆盖。
        """
        import csv
        import json as _json

        try:
            items = []
            page = 1
            while True:
                data = db.query_works(path=user_db, batch_id=batch_id,
                                      sort="priority", page=page, per_page=200)
                items.extend(data.get("items") or [])
                if page >= data.get("pages", 1):
                    break
                page += 1
            run = next((b for b in db.list_batches(user_db)
                        if int(b["run_id"]) == int(batch_id)), {})
            manifest = {
                "batch_id": batch_id,
                "source_url": run.get("url", ""),
                "started_at": run.get("started_at", ""),
                "finished_at": run.get("finished_at", ""),
                "work_count": len(items),
                "works": [
                    {
                        "gallery_id": it["gallery_id"],
                        "title": it["title"],
                        "title_original": it.get("title_original", ""),
                        "languages": [lang["key"] for lang in it.get("languages", [])],
                        "num_pages": it.get("num_pages"),
                        "uncensored": bool(it.get("uncensored")),
                        "url": it.get("url", ""),
                    }
                    for it in items
                ],
            }
            (directory / f"manifest_batch_{batch_id}.json").write_text(
                _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            with (directory / f"works_batch_{batch_id}.csv").open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["gallery_id", "title", "title_original", "languages",
                                 "num_pages", "uncensored", "url"])
                for row in manifest["works"]:
                    writer.writerow([row["gallery_id"], row["title"], row["title_original"],
                                     " ".join(row["languages"]), row["num_pages"],
                                     int(row["uncensored"]), row["url"]])
        except Exception as exc:
            traceback.print_exc()

    @staticmethod
    def _existing_file(download_dir: Path, gallery_id: int, fmt: str):
        ext = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
        candidates = [f for f in download_dir.glob(f"{int(gallery_id)}_*.{ext}")
                      if f.is_file() and f.stat().st_size > 0]
        legacy = download_dir / f"{int(gallery_id)}.{ext}"
        if legacy.is_file() and legacy.stat().st_size > 0:
            candidates.append(legacy)
        return candidates[0] if candidates else None

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass