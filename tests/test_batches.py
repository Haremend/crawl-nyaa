"""批次号 / 重复抓取不覆盖原始记录 测试。"""
import shutil
import unittest
import uuid
from pathlib import Path

from app import db
from app.parser import GalleryItem

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def item(gid=1001, title="Work A", language="japanese", **extra):
    return GalleryItem(gallery_id=gid, url=f"https://example.net/g/{gid}/",
                       title=title, languages=[language], **extra)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("batch")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_recorded_and_filtered(self):
        db.upsert_items([item(1)], source_url="https://example.net/group/a/",
                        batch_id=7, path=self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["batch_id"], 7)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50, batch_id=7)["total"], 1)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50, batch_id=8)["total"], 0)

    def test_same_data_keeps_original_batch(self):
        """第二批抓到完全相同的数据时，保留第一批的记录与批次号。"""
        db.upsert_items([item(1)], source_url="https://example.net/group/a/",
                        batch_id=7, path=self.db_path)
        result = db.upsert_items([item(1)], source_url="https://example.net/group/b/",
                                 batch_id=9, path=self.db_path)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["unchanged"], 1, "数据相同应计为未变化")

        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["batch_id"], 7, "批次号应保持第一次的值")
        self.assertEqual(data["items"][0]["last_seen"][:10],
                         db.query_works(path=self.db_path, per_page=50)["items"][0]["last_seen"][:10])

    def test_changed_data_updates_record(self):
        db.upsert_items([item(1, title="Old")], source_url="https://example.net/group/a/",
                        batch_id=7, path=self.db_path)
        result = db.upsert_items([item(1, title="New")], source_url="https://example.net/group/a/",
                                 batch_id=8, path=self.db_path)
        self.assertEqual(result["unchanged"], 0)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["title"], "New")
        self.assertEqual(data["items"][0]["batch_id"], 8, "数据变化时批次号更新为最新")

    def test_richer_record_not_overwritten_by_listing(self):
        """已有详情数据的记录，被后续仅有列表数据的抓取命中时不应判为变化。"""
        db.upsert_items([{"gallery_id": 2, "url": "https://example.net/g/2/", "title": "Rich",
                          "tags": ["uncensored"], "artists": ["someone"],
                          "languages": ["japanese"], "batch_id": 3, "num_pages": 30}],
                        source_url="https://example.net/group/a/", path=self.db_path)
        result = db.upsert_items([item(2, title="Rich", language="japanese")],
                                 source_url="https://example.net/group/a/",
                                 batch_id=11, path=self.db_path)
        self.assertEqual(result["unchanged"], 1, "列表数据与库中一致，不应覆盖详情数据")
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["batch_id"], 3)
        self.assertIn("uncensored", data["items"][0]["tags"])

    def test_list_batches_reports_url_and_count(self):
        run_id = db.create_run("https://example.net/group/a/", self.db_path)
        db.upsert_items([item(3), item(4)], source_url="https://example.net/group/a/",
                        batch_id=run_id, path=self.db_path)
        batches = db.list_batches(self.db_path)
        self.assertTrue(batches)
        target = next(b for b in batches if b["run_id"] == run_id)
        self.assertEqual(target["url"], "https://example.net/group/a/")
        self.assertEqual(target["works"], 2, "批次应统计本批入库的作品数")

    def test_list_batch_targets_picks_recommended_variant(self):
        run_id = db.create_run("https://example.net/group/a/", self.db_path)
        db.upsert_items([item(10, title="Same", language="english"),
                         item(11, title="Same", language="japanese")],
                        source_url="https://example.net/group/a/",
                        batch_id=run_id, path=self.db_path)
        targets = db.list_batch_targets(run_id, self.db_path)
        self.assertEqual(len(targets), 1, "同一作品只应产生一个下载目标")
        self.assertEqual(targets[0]["gallery_id"], 11, "应取推荐版本（日语）")

    def test_list_batch_targets_empty_for_unknown_batch(self):
        self.assertEqual(db.list_batch_targets(999, self.db_path), [])


class BatchSaveTests(unittest.TestCase):
    """「保存全部」：批次模式下载服务的参数校验。"""

    def setUp(self):
        self.tmp = make_temp_dir("saveall")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_api_key(self):
        from app.downloader import DownloadService
        from app.upstream import UpstreamError
        service = DownloadService(self.db_path)
        with self.assertRaises(UpstreamError) as ctx:
            service.start("", "zip", mode="batch", batch_id=1)
        self.assertEqual(ctx.exception.status, 401)

    def test_requires_batch_id(self):
        from app.downloader import DownloadService
        from app.upstream import UpstreamError
        db.create_run("https://example.net/group/a/", self.db_path)
        service = DownloadService(self.db_path)
        with self.assertRaises(UpstreamError) as ctx:
            service.start("dummy", "zip", mode="batch", batch_id=0)
        self.assertIn("批次", str(ctx.exception))

    def test_empty_batch_rejected(self):
        from app.downloader import DownloadService
        from app.upstream import UpstreamError
        run_id = db.create_run("https://example.net/group/a/", self.db_path)
        service = DownloadService(self.db_path)
        with self.assertRaises(UpstreamError) as ctx:
            service.start("dummy", "zip", mode="batch", batch_id=run_id)
        self.assertIn("没有可保存", str(ctx.exception))



if __name__ == "__main__":
    unittest.main()