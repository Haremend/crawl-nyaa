"""抓取服务测试：任务启动不能死锁、参数校验、进度快照。"""
import shutil
import threading
import time
import unittest
import uuid
from pathlib import Path

from app import config, db, scrape_service as service_module
from app.parser import GalleryItem
from app.scraper import PageResult
from app.scrape_service import ScrapeError, ScrapeService, normalize_listing_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


from app import config as _lock_cfg, supabase_client as _lock_sc
_lock_cfg.STORAGE_BACKEND = "local"
_lock_cfg.SUPABASE_PROJECT_REF = ""
_lock_cfg.SUPABASE_TOKEN = ""
_lock_sc.set_transport(None)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class NormalizeUrlTests(unittest.TestCase):
    def test_group_url(self):
        self.assertEqual(
            normalize_listing_url("https://example.net/group/samplecircle/?page=3"),
            "https://example.net/group/samplecircle/",
        )

    def test_missing_scheme_is_completed(self):
        self.assertEqual(
            normalize_listing_url("example.net/tag/ahegao/"),
            "https://example.net/tag/ahegao/",
        )

    def test_accepts_any_host(self):
        # 域名检查已移除
        self.assertEqual(
            normalize_listing_url("https://example.com/group/x/"),
            "https://example.com/group/x/",
        )

    def test_rejects_detail_pages(self):
        with self.assertRaises(ScrapeError):
            normalize_listing_url("https://example.net/g/123/")


class ServiceStartTests(unittest.TestCase):
    """回归测试：start() 持锁期间会调用 _log()，若锁不可重入会自我死锁。"""

    def setUp(self):
        self.tmp = make_temp_dir("service")
        self.db_path = self.tmp / "test.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_returns_quickly_with_reentrant_lock(self):
        original_iter_pages = service_module.iter_pages

        def fake_iter_pages(url, **_kwargs):
            yield PageResult(
                page_no=1,
                url=url,
                items=[GalleryItem(gallery_id=1, url="https://example.net/g/1/", title="x")],
                last_page=1,
                has_next=False,
            )

        service_module.iter_pages = fake_iter_pages
        try:
            scrape = ScrapeService(self.db_path)
            started = time.monotonic()
            snapshot = scrape.start("https://example.net/group/samplecircle/", max_pages=1, enrich=False)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "start() 不应阻塞（曾因非重入锁死锁）")
            self.assertTrue(snapshot["running"])
            self.assertEqual(snapshot["url"], "https://example.net/group/samplecircle/")
            self.assertTrue(snapshot["logs"], "启动日志应已写入")

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                snapshot = scrape.snapshot()
                if not snapshot["running"]:
                    break
                time.sleep(0.05)
            self.assertFalse(snapshot["running"], "任务应在后台正常结束")
            self.assertEqual(snapshot["status"], "done")
            self.assertEqual(snapshot["items_seen"], 1)
        finally:
            service_module.iter_pages = original_iter_pages

    def test_second_task_is_queued_while_running(self):
        """队列出队：第一个任务在跑时，第二个 start 不再拒绝，而是入队等候。"""
        original_iter_pages = service_module.iter_pages
        release = []

        def slow_iter_pages(url, **_kwargs):
            # 首任务在 release 前阻塞，让 start() 能在「运行中」再发一个
            if not release:
                while not release:
                    time.sleep(0.05)
            yield PageResult(page_no=1, url=url, items=[], last_page=1, has_next=False)

        service_module.iter_pages = slow_iter_pages
        try:
            scrape = ScrapeService(self.db_path)
            s1 = scrape.start("https://example.net/group/samplecircle/",
                              max_pages=1, enrich=False)
            self.assertTrue(s1["running"])
            self.assertEqual(s1["url"], "https://example.net/group/samplecircle/")
            # 第二个 start：不再抛 ScrapeError，而是入队
            s2 = scrape.start("https://example.net/group/another/",
                              max_pages=1, enrich=False)
            self.assertTrue(s2["running"], "仍在跑第一个任务")
            self.assertEqual(s2["url"], "https://example.net/group/samplecircle/",
                             "url 反映的是当前运行任务，不是刚入队的")
            self.assertGreaterEqual(s2["queue_length"], 1, "第二个任务应排在队列里")
            queued_urls = [t["url"] for t in s2["queue"]]
            self.assertIn("https://example.net/group/another/", queued_urls)
            release.append(True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and scrape.snapshot()["running"]:
                time.sleep(0.05)
            snap = scrape.snapshot()
            self.assertFalse(snap["running"], "两个任务都应跑完")
            self.assertEqual(snap["queue_length"], 0)
        finally:
            service_module.iter_pages = original_iter_pages
            release.append(True)

    def test_cancel_clears_queue_and_stops(self):
        """取消 = 停当前任务 + 清空待抓取队列。"""
        original_iter_pages = service_module.iter_pages
        release = []

        def slow_iter_pages(url, **_kwargs):
            while not release:
                time.sleep(0.05)
            yield PageResult(page_no=1, url=url, items=[], last_page=1, has_next=False)

        service_module.iter_pages = slow_iter_pages
        try:
            scrape = ScrapeService(self.db_path)
            scrape.start("https://example.net/group/a/", max_pages=1, enrich=False)  # 在跑
            scrape.start("https://example.net/group/b/", max_pages=1, enrich=False)  # 排队
            scrape.start("https://example.net/group/c/", max_pages=1, enrich=False)  # 排队
            snap = scrape.snapshot()
            self.assertGreaterEqual(snap["queue_length"], 1, "b/c 应在队列里")
            cancelled = scrape.cancel()
            self.assertTrue(cancelled, "应有任务被取消")
            snap = scrape.snapshot()
            self.assertEqual(snap["queue_length"], 0, "取消应清空整个队列")
            release.append(True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and scrape.snapshot()["running"]:
                time.sleep(0.05)
            self.assertFalse(scrape.snapshot()["running"], "worker 应停止")
        finally:
            service_module.iter_pages = original_iter_pages
            release.append(True)

    def test_tasks_run_sequentially(self):
        """多个任务入队后必须串行执行（同时只有 1 个在跑）。"""
        original_iter_pages = service_module.iter_pages
        active = [0]
        max_active = [0]
        guard = threading.Lock()

        def tracking_iter_pages(url, **_kwargs):
            with guard:
                active[0] += 1
                max_active[0] = max(max_active[0], active[0])
            time.sleep(0.1)  # 模拟抓取耗时，放大并发窗口
            with guard:
                active[0] -= 1
            yield PageResult(
                page_no=1, url=url,
                items=[GalleryItem(gallery_id=abs(hash(url)) % 100000,
                                   url=url + "1/", title="x")],
                last_page=1, has_next=False)

        service_module.iter_pages = tracking_iter_pages
        try:
            scrape = ScrapeService(self.db_path)
            for i in range(3):
                snap = scrape.start(f"https://example.net/group/g{i}/",
                                   max_pages=1, enrich=False)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and scrape.snapshot()["running"]:
                time.sleep(0.05)
            self.assertEqual(max_active[0], 1, "任务必须逐个串行执行，不能并发")
            snap = scrape.snapshot()
            self.assertFalse(snap["running"])
            self.assertEqual(snap["queue_length"], 0)
        finally:
            service_module.iter_pages = original_iter_pages


if __name__ == "__main__":
    unittest.main()