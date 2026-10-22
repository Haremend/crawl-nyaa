"""抓取服务测试：任务启动不能死锁、参数校验、进度快照。"""
import shutil
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

    def test_second_task_is_rejected_while_running(self):
        original_iter_pages = service_module.iter_pages
        release = []

        def slow_iter_pages(url, **_kwargs):
            while not release:
                time.sleep(0.05)
            yield PageResult(page_no=1, url=url, items=[], last_page=1, has_next=False)

        service_module.iter_pages = slow_iter_pages
        try:
            scrape = ScrapeService(self.db_path)
            scrape.start("https://example.net/group/samplecircle/", max_pages=1, enrich=False)
            with self.assertRaises(ScrapeError):
                scrape.start("https://example.net/group/samplecircle/", max_pages=1, enrich=False)
            release.append(True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and scrape.snapshot()["running"]:
                time.sleep(0.05)
        finally:
            service_module.iter_pages = original_iter_pages
            release.append(True)


if __name__ == "__main__":
    unittest.main()