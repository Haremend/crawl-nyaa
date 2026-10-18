"""一键下载：批量下载所有「待下载」作品。

流程：读取标记为待下载的作品 → 用用户 API Key 取下载链接 → 下载文件到
项目 download/ 目录 → 在数据库中标记「已下载」。

- API Key 只用于「取下载链接」这一步，且只存在于任务运行期间；
- 文件下载走公开 CDN，不需要 Key；
- 默认格式 zip（torrent 的限流很严：5/分钟/IP、10/5分钟/用户）。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .scraper import FetchError, Fetcher
from .upstream import UpstreamClient, UpstreamError

FORMAT_EXT = {"zip": "zip", "cbz": "cbz", "torrent": "torrent"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DownloadService:
    """单任务下载服务（同一时间只允许一个批量下载任务）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: list[str] = []
        self._state: dict = self._idle_state()
        db.init_db(db_path)

    # ---------- 对外接口 ----------
    def start(self, api_key: str, fmt: str = "zip", *,
              mode: str = "want", batch_id: int | None = None) -> dict:
        """mode="want"：下载所有「待下载」；mode="batch"：保存指定批次的全部作品。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise UpstreamError("已有下载任务正在进行", 409)
            key = (api_key or "").strip()
            if not key:
                raise UpstreamError("缺少 API Key", 401)
            origin = db.get_site_origin(self.db_path)
            if not origin:
                raise UpstreamError("未知站点地址：请先抓取一次列表页", 400)

            if mode == "batch":
                batch = int(batch_id or 0)
                if batch <= 0:
                    raise UpstreamError("请先选择一个批次", 400)
                targets = db.list_batch_targets(batch, self.db_path)
                if not targets:
                    raise UpstreamError(f"批次 #{batch} 没有可保存的作品", 400)
                subdir = f"batch_{batch}"
            else:
                batch = None
                targets = db.list_want_download_targets(self.db_path)
                if not targets:
                    raise UpstreamError("没有标记为「待下载」的作品", 400)
                subdir = ""
            fmt = fmt if fmt in FORMAT_EXT else "zip"

            self._cancel = threading.Event()
            self._logs = []
            self._state = {
                "status": "running",
                "running": True,
                "format": fmt,
                "mode": mode,
                "batch_id": batch,
                "out_dir": str(Path(config.DOWNLOADS_DIR) / subdir) if subdir else str(config.DOWNLOADS_DIR),
                "total": len(targets),
                "done": 0,
                "failed": 0,
                "current": "",
                "started_at": _now(),
                "finished_at": None,
                "error": None,
            }
            scope = f"批次 #{batch}" if mode == "batch" else "待下载列表"
            self._log(f"开始下载 {scope} 的 {len(targets)} 部作品（格式 {fmt}）")
            self._thread = threading.Thread(
                target=self._run, args=(key, origin, fmt, targets, subdir, mode, batch),
                name="gallery-download", daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def cancel(self) -> bool:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
        if alive:
            self._cancel.set()
            self._log("收到取消请求，当前文件下载完后停止")
        return alive

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            logs = list(self._logs)
            alive = self._thread is not None and self._thread.is_alive()
        state["logs"] = logs[-80:]
        state["running"] = bool(alive)
        if alive:
            state["status"] = "running"
        return state

    # ---------- 内部 ----------
    @staticmethod
    def _idle_state() -> dict:
        return {
            "status": "idle", "running": False, "format": "zip",
            "total": 0, "done": 0, "failed": 0, "current": "",
            "started_at": None, "finished_at": None, "error": None,
        }

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"[{stamp}] {message}")

    def _progress(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)

    def _write_manifest(self, directory: Path, batch_id: int | None,
                        targets: list[dict], origin: str) -> None:
        """在批次目录里写一份清单（JSON + CSV），记录本批次抓取来源与作品列表。"""
        import csv
        import json as _json

        rows = db.query_works(per_page=200, batch_id=batch_id, sort="priority", path=self.db_path)
        items = []
        page = 1
        while page <= rows.get("pages", 1):
            if page > 1:
                rows = db.query_works(page=page, per_page=200, batch_id=batch_id,
                                      sort="priority", path=self.db_path)
            items.extend(rows["items"])
            page += 1

        run = next((b for b in db.list_batches(self.db_path)
                    if int(b["run_id"]) == int(batch_id or 0)), {})
        manifest = {
            "batch_id": batch_id,
            "source_url": run.get("url", ""),
            "started_at": run.get("started_at", ""),
            "finished_at": run.get("finished_at", ""),
            "site_origin": origin,
            "work_count": len(items),
            "works": [
                {
                    "gallery_id": it["gallery_id"],
                    "title": it["title"],
                    "title_original": it.get("title_original", ""),
                    "languages": [lang["key"] for lang in it.get("languages", [])],
                    "num_pages": it.get("num_pages"),
                    "num_favorites": it.get("num_favorites"),
                    "uncensored": bool(it.get("uncensored")),
                    "url": it.get("url", ""),
                    "tags": it.get("tags", []),
                }
                for it in items
            ],
        }
        try:
            (directory / "manifest.json").write_text(
                _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            with (directory / "works.csv").open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["gallery_id", "title", "title_original", "languages",
                                 "num_pages", "num_favorites", "uncensored", "url"])
                for row in manifest["works"]:
                    writer.writerow([row["gallery_id"], row["title"], row["title_original"],
                                     " ".join(row["languages"]), row["num_pages"],
                                     row["num_favorites"], int(row["uncensored"]), row["url"]])
            self._log(f"已写入清单：manifest.json / works.csv（{len(items)} 部作品）")
        except OSError as exc:
            self._log(f"清单写入失败：{exc}")

    def _pause(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0 and not self._cancel.is_set():
            step = min(0.1, remaining)
            time.sleep(step)
            remaining -= step

    def _run(self, api_key: str, origin: str, fmt: str, targets: list[dict],
             subdir: str = "", mode: str = "want", batch_id: int | None = None) -> None:
        done = failed = 0
        status = "done"
        error: str | None = None
        download_dir = Path(config.DOWNLOADS_DIR)
        if subdir:
            download_dir = download_dir / subdir
        download_dir.mkdir(parents=True, exist_ok=True)
        fetcher = Fetcher()
        client = UpstreamClient(origin, api_key, fetcher=fetcher)
        ext = FORMAT_EXT.get(fmt, "zip")

        try:
            if mode == "batch":
                self._write_manifest(download_dir, batch_id, targets, origin)
            for index, target in enumerate(targets, start=1):
                if self._cancel.is_set():
                    status = "cancelled"
                    break
                gallery_id = int(target["gallery_id"])
                title = str(target.get("title") or f"#{gallery_id}")
                self._progress(current=f"#{gallery_id} {title[:40]}")
                self._log(f"[{index}/{len(targets)}] 取下载链接 #{gallery_id}")
                try:
                    info = client.get_download_url(gallery_id, fmt)
                except UpstreamError as exc:
                    failed += 1
                    self._log(f"  取链接失败：{exc}")
                    self._progress(done=done, failed=failed)
                    self._pause(config.DOWNLOAD_DELAY)
                    continue

                url = str(info.get("url") or "")
                if not url:
                    failed += 1
                    self._log("  上游未返回下载地址")
                    self._progress(done=done, failed=failed)
                    self._pause(config.DOWNLOAD_DELAY)
                    continue

                target_path = download_dir / f"{gallery_id}.{ext}"
                if target_path.exists() and target_path.stat().st_size > 0:
                    self._log("  已存在同名文件，跳过下载")
                    db.set_downloaded([gallery_id], True, self.db_path)
                    done += 1
                    self._progress(done=done, failed=failed)
                    continue

                try:
                    body, _ctype = fetcher.request(url, referer=f"{origin}/g/{gallery_id}/")
                except FetchError as exc:
                    failed += 1
                    self._log(f"  下载失败：{exc}")
                    self._progress(done=done, failed=failed)
                    self._pause(config.DOWNLOAD_DELAY)
                    continue

                try:
                    target_path.write_bytes(body)
                except OSError as exc:
                    failed += 1
                    self._log(f"  保存失败：{exc}")
                    self._progress(done=done, failed=failed)
                    self._pause(config.DOWNLOAD_DELAY)
                    continue

                db.set_downloaded([gallery_id], True, self.db_path)
                done += 1
                self._log(f"  已保存 {target_path.name}（{len(body) / 1048576:.1f} MB）")
                self._progress(done=done, failed=failed)
                self._pause(config.DOWNLOAD_DELAY)

            if status != "cancelled":
                self._log(f"完成：成功 {done}，失败 {failed}")
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"任务异常：{error}")
            traceback.print_exc()
        finally:
            self._progress(
                status=status, running=False, finished_at=_now(),
                done=done, failed=failed, current="", error=error,
            )