"""封面下载模块测试：文件名安全、扩展名推断、幂等、下载与记录。"""
import shutil
import unittest
import uuid
from pathlib import Path

from app import covers, db
from app.detail import parse_detail_payload
from app.parser import parse_listing
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)
SAMPLES = PROJECT_ROOT / "samples"


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class FakeFetcher:
    """记录调用并返回固定图片字节，避免测试联网。"""

    def __init__(self, body=b"RIFFxxxxWEBP", content_type="image/webp", fail=False):
        self.body = body
        self.content_type = content_type
        self.fail = fail
        self.calls = []

    def fetch_bytes(self, url, referer=None):
        self.calls.append((url, referer))
        if self.fail:
            from app.scraper import FetchError
            raise FetchError("boom")
        return self.body, self.content_type


class CoverHelpersTests(unittest.TestCase):
    def test_media_url_validation(self):
        self.assertTrue(covers.is_media_url("https://t3.example.net/galleries/1/1t.jpg"))
        self.assertTrue(covers.is_media_url("https://example.net/x.webp"))
        # is_media_url 现在只检查 http/https scheme，不按域名拒绝
        self.assertTrue(covers.is_media_url("https://evil.example.com/a.jpg"))
        self.assertFalse(covers.is_media_url("file:///c:/a.jpg"))
        self.assertFalse(covers.is_media_url(""))

    def test_extension_from_url_then_content_type(self):
        self.assertEqual(covers.extension_for("https://t1.example.net/galleries/1/1t.jpg", ""), "jpg")
        self.assertEqual(covers.extension_for("https://t1.example.net/galleries/1/1t.jpeg", ""), "jpg")
        self.assertEqual(covers.extension_for("https://t1.example.net/galleries/1/thumb.webp", "image/jpeg"), "webp")
        self.assertEqual(covers.extension_for("https://t1.example.net/galleries/1/thumb", "image/png"), "png")
        self.assertEqual(covers.extension_for("https://t1.example.net/galleries/1/thumb", ""), "webp")

    def test_cover_filename(self):
        self.assertEqual(covers.cover_filename(123, "webp"), "123.webp")
        self.assertEqual(covers.cover_filename(123, "jpeg"), "123.jpg")
        self.assertEqual(covers.cover_filename(123, "bogus"), "123.webp")

    def test_safe_filename_blocks_traversal(self):
        self.assertTrue(covers.is_safe_filename("123.webp"))
        self.assertTrue(covers.is_safe_filename("123.jpg"))
        for bad in ("../123.webp", "..\\123.jpg", "a/b.webp", "", ".hidden", "a b.webp"):
            self.assertFalse(covers.is_safe_filename(bad), bad)


class CoverDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("cover")
        self.cover_dir = self.tmp / "cover"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_writes_file(self):
        fetcher = FakeFetcher()
        name = covers.download_cover(
            9900001, "https://t3.example.net/galleries/9900001/thumb.webp",
            cover_dir=self.cover_dir, fetcher=fetcher,
        )
        self.assertEqual(name, "9900001.webp")
        self.assertEqual((self.cover_dir / name).read_bytes(), fetcher.body)
        self.assertEqual(len(fetcher.calls), 1)

    def test_second_call_is_idempotent(self):
        fetcher = FakeFetcher()
        url = "https://t3.example.net/galleries/9900001/thumb.webp"
        covers.download_cover(9900001, url, cover_dir=self.cover_dir, fetcher=fetcher)
        covers.download_cover(9900001, url, cover_dir=self.cover_dir, fetcher=fetcher)
        self.assertEqual(len(fetcher.calls), 1, "已有封面时不应重复请求")

    def test_rejects_non_http(self):
        fetcher = FakeFetcher()
        name = covers.download_cover(1, "file:///c:/a.jpg",
                                     cover_dir=self.cover_dir, fetcher=fetcher)
        self.assertIsNone(name)
        self.assertEqual(fetcher.calls, [])
        name = covers.download_cover(1, "",
                                     cover_dir=self.cover_dir, fetcher=fetcher)
        self.assertIsNone(name)

    def test_failure_returns_none(self):
        name = covers.download_cover(
            1, "https://t3.example.net/galleries/1/1t.jpg",
            cover_dir=self.cover_dir, fetcher=FakeFetcher(fail=True),
        )
        self.assertIsNone(name)


class CoverDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("coverdb")
        self.db_path = self.tmp / "test.sqlite3"
        self.cover_dir = self.tmp / "cover"
        db.init_db(self.db_path)
        page = parse_listing(
            (SAMPLES / "listing_new_demo.html").read_text(encoding="utf-8"),
            "https://example.net/group/samplecircle/",
        )
        db.upsert_items(page.items, source_url="demo", path=self.db_path)
        self.ids = [item.gallery_id for item in page.items]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ids_needing_cover_then_cleared(self):
        self.assertEqual(sorted(db.ids_needing_cover(self.ids, self.db_path)), sorted(self.ids))
        fetcher = FakeFetcher()
        url = db.cover_urls([910004], self.db_path)[910004]
        name = covers.download_cover(910004, url, cover_dir=self.cover_dir, fetcher=fetcher)
        db.set_cover(910004, name, self.db_path)
        remaining = db.ids_needing_cover(self.ids, self.db_path)
        self.assertNotIn(910004, remaining)

    def test_cover_survives_reupsert(self):
        db.set_cover(910004, "910004.webp", self.db_path)
        # 重新抓取列表页（不含封面字段）不应清掉本地封面记录
        page = parse_listing(
            (SAMPLES / "listing_new_demo.html").read_text(encoding="utf-8"),
            "https://example.net/group/samplecircle/",
        )
        db.upsert_items(page.items, source_url="demo2", path=self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        target = next(item for item in data["items"] if 910004 in item["ids"])
        self.assertEqual(target["cover_file"], "910004.webp")

    def test_stats_counts_covers(self):
        db.set_cover(910004, "910004.webp", self.db_path)
        self.assertEqual(db.stats(self.db_path)["covers"], 1)


class OriginalSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("sortorig")
        self.db_path = self.tmp / "test.sqlite3"
        db.init_db(self.db_path)
        page = parse_listing(
            (SAMPLES / "listing_new_demo.html").read_text(encoding="utf-8"),
            "https://example.net/group/samplecircle/",
        )
        db.upsert_items(page.items, source_url="demo", path=self.db_path)
        payloads = [
            json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8")),
            json.loads((SAMPLES / "detail_910004.json").read_text(encoding="utf-8")),
        ]
        db.apply_details([parse_detail_payload(payload) for payload in payloads], self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sort_by_original_title(self):
        data = db.query_works(path=self.db_path, sort="original", per_page=50)
        keys = [db._original_sort_key(item) for item in data["items"]]
        self.assertEqual(keys, sorted(keys))
        # 原名称（日文）应当参与排序：有原始标题的排在前
        self.assertTrue(data["items"][0]["title_original"])

    def test_unknown_sort_falls_back_to_recent(self):
        data = db.query_works(path=self.db_path, sort="not-a-sort", per_page=50)
        ids = [item["gallery_id"] for item in data["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))


if __name__ == "__main__":
    unittest.main()