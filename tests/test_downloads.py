"""待下载 / 已下载 / 站点收藏 / 下载服务测试（全部离线）。"""
import shutil
import time
import unittest
import uuid
from pathlib import Path

from app import db
from app.downloader import DownloadService
from app.parser import GalleryItem
from app.upstream import UpstreamClient, UpstreamError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class WantDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("want")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        db.upsert_items(
            [
                GalleryItem(gallery_id=1001, url="https://example.net/g/1001/", title="Work A",
                            languages=["japanese"], title_key="worka"),
                GalleryItem(gallery_id=1002, url="https://example.net/g/1002/", title="Work B",
                            languages=["english"], title_key="workb"),
            ],
            source_url="https://example.net/group/x/", path=self.db_path,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mark_and_filter_want_download(self):
        self.assertEqual(db.set_want_download([1001], True, self.db_path), 1)
        data = db.query_works(path=self.db_path, per_page=50)
        flags = {item["gallery_id"]: item["want_download"] for item in data["items"]}
        self.assertTrue(flags[1001])
        self.assertFalse(flags[1002])

        only = db.query_works(path=self.db_path, per_page=50, want_download_only=True)
        self.assertEqual(only["total"], 1)
        self.assertEqual(only["items"][0]["gallery_id"], 1001)

        db.set_want_download([1001], False, self.db_path)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50, want_download_only=True)["total"], 0)

    def test_mark_downloaded_and_filter(self):
        self.assertEqual(db.set_downloaded([1002], True, self.db_path), 1)
        data = db.query_works(path=self.db_path, per_page=50, downloaded_only=True)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["gallery_id"], 1002)
        self.assertTrue(data["items"][0]["downloaded"])
        self.assertTrue(data["items"][0]["downloaded_at"])

        db.set_downloaded([1002], False, self.db_path)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50, downloaded_only=True)["total"], 0)

    def test_stats_counts(self):
        db.set_want_download([1001], True, self.db_path)
        db.set_downloaded([1002], True, self.db_path)
        info = db.stats(self.db_path)
        self.assertEqual(info["want_download"], 1)
        self.assertEqual(info["downloaded"], 1)

    def test_targets_use_recommended_variant(self):
        # 同一作品两个语言版本，只标记其中一个版本为空时，目标应取推荐版本
        db.upsert_items(
            [
                GalleryItem(gallery_id=2001, url="https://example.net/g/2001/", title="Same",
                            languages=["english"], title_key="same"),
                GalleryItem(gallery_id=2002, url="https://example.net/g/2002/", title="Same",
                            languages=["japanese"], title_key="same"),
            ],
            source_url="demo", path=self.db_path,
        )
        db.set_want_download([2001], True, self.db_path)   # 标记英文版
        targets = db.list_want_download_targets(self.db_path)
        ids = [t["gallery_id"] for t in targets]
        self.assertIn(2002, ids, "应下载推荐版本（日语）而不是被标记的英文版")

    def test_site_favorites_filter(self):
        db.upsert_items(
            [{"gallery_id": 3001, "url": "https://example.net/g/3001/", "title": "Fav",
              "site_favorite": 1, "from_scrape": 0, "languages": ["japanese"]}],
            source_url="demo", path=self.db_path,
        )
        data = db.query_works(path=self.db_path, per_page=50, site_favorites_only=True)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["gallery_id"], 3001)
        self.assertTrue(data["items"][0]["site_favorite"])


class UpstreamClientTests(unittest.TestCase):
    """离线校验：不联网，只验证 Key 使用规则与错误处理。"""

    def test_auth_required_without_key(self):
        client = UpstreamClient("https://example.net", "")
        with self.assertRaises(UpstreamError) as ctx:
            client.get_favorites(page=1)
        self.assertEqual(ctx.exception.status, 401)

        with self.assertRaises(UpstreamError) as ctx2:
            client.get_download_url(1, "zip")
        self.assertEqual(ctx2.exception.status, 401)

    def test_missing_origin(self):
        client = UpstreamClient("", "dummy-key")
        with self.assertRaises(UpstreamError) as ctx:
            client.get_gallery(1)
        self.assertEqual(ctx.exception.status, 500)

    def test_public_calls_never_send_key(self):
        """公开接口不应携带 Authorization 头（用户要求）。"""
        seen = {}

        class FakeFetcher:
            def request(self, url, *, method="GET", headers=None, data=None, referer=None):
                seen["url"] = url
                seen["headers"] = dict(headers or {})
                return b"{}", "application/json"

        client = UpstreamClient("https://example.net", "secret-key", fetcher=FakeFetcher())
        client.get_gallery(123)
        self.assertNotIn("Authorization", seen["headers"], "公开接口不应带 Key")

        client.resolve_tags([1, 2])
        self.assertNotIn("Authorization", seen["headers"], "标签解析是公开接口，不应带 Key")

    def test_auth_calls_do_send_key(self):
        seen = {}

        class FakeFetcher:
            def request(self, url, *, method="GET", headers=None, data=None, referer=None):
                seen["headers"] = dict(headers or {})
                seen["method"] = method
                return b'{"result": []}', "application/json"

        client = UpstreamClient("https://example.net", "secret-key", fetcher=FakeFetcher())
        client.get_favorites(page=1)
        self.assertEqual(seen["headers"].get("Authorization"), "Key secret-key")

        client.get_download_url(5, "zip")
        self.assertEqual(seen["headers"].get("Authorization"), "Key secret-key")
        self.assertEqual(seen["method"], "POST")


class DownloadServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("dl")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_api_key(self):
        service = DownloadService(self.db_path)
        with self.assertRaises(UpstreamError) as ctx:
            service.start("", "zip")
        self.assertEqual(ctx.exception.status, 401)

    def test_requires_targets(self):
        db.upsert_items(
            [GalleryItem(gallery_id=1, url="https://example.net/g/1/", title="x",
                         languages=["japanese"])],
            source_url="https://example.net/group/x/", path=self.db_path,
        )
        service = DownloadService(self.db_path)
        with self.assertRaises(UpstreamError) as ctx:
            service.start("dummy-key", "zip")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("待下载", str(ctx.exception))

    def test_idle_snapshot(self):
        service = DownloadService(self.db_path)
        snapshot = service.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["status"], "idle")




class DownloadQueueSelfHealTests(unittest.TestCase):
    """download_jobs 表缺失时 worker 不应死亡，应自愈建表后继续轮询。

    回归测试：早期 _worker 把 db.claim_next_job() 放在 try/except 之外，
    一旦 download_jobs 表不存在（DB 未初始化 / 迁移未完成），SELECT 抛
    OperationalError 会直接逃出 while 循环，worker 线程永久死亡，
    5 个全死则整队静默停摆。
    """

    def setUp(self):
        self.tmp = make_temp_dir("dqheal")
        self.db_path = self.tmp / "t.sqlite3"
        # 只创建 DB 文件、不建任何表，模拟“DB 未初始化”场景
        conn = db.connect(self.db_path)
        conn.close()
        self.queue = None

    def tearDown(self):
        if self.queue is not None:
            self.queue.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worker_self_heals_missing_table(self):
        from app.download_queue import DownloadQueue
        self.queue = DownloadQueue(self.db_path, workers=2)
        self.queue.start()
        # worker 启动后立即应处于存活状态
        self.assertTrue(self.queue.running)

        # 自愈需要在 worker 首次轮询后发生；最多等 5 秒
        healed = False
        for _ in range(25):
            conn = db.connect(self.db_path)
            try:
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            except Exception:
                names = set()
            finally:
                conn.close()
            if "download_jobs" in names:
                healed = True
                break
            time.sleep(0.2)

        self.assertTrue(healed, "自愈后 download_jobs 表应存在")
        # 自愈后 worker 仍存活（没有因缺表而死亡）
        self.assertTrue(self.queue.running)


if __name__ == "__main__":
    unittest.main()