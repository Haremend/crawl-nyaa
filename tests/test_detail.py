"""详情 API 解析、详情补全入库与排序测试（使用离线 JSON 样本）。"""
import json
import shutil
import unittest
import uuid
from pathlib import Path

from app import db
from app.detail import parse_detail_payload
from app.parser import parse_listing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)
SAMPLES = PROJECT_ROOT / "samples"


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class DetailPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8"))

    def test_parse_fields(self):
        detail = parse_detail_payload(self.payload)
        self.assertEqual(detail.gallery_id, 910001)
        self.assertEqual(detail.media_id, 9900003)
        self.assertEqual(detail.num_pages, 32)
        self.assertEqual(detail.num_favorites, 967)
        self.assertTrue(detail.upload_date.startswith("2025-"))
        self.assertEqual(detail.languages, ["japanese"])
        self.assertIn("big breasts", detail.tags)
        self.assertIn("sampleartist1", detail.artists)
        self.assertIn("samplecircle", detail.groups)
        self.assertIn("zenless zone zero", detail.parodies)

    def test_pseudo_language_tags_are_ignored(self):
        payload = json.loads((SAMPLES / "detail_910004.json").read_text(encoding="utf-8"))
        detail = parse_detail_payload(payload)
        self.assertEqual(detail.languages, ["english"])

    def test_thumbnail_object_is_normalized_to_url(self):
        # 真实 API 的 thumbnail 是对象；旧实现会 str() 成脏字符串并覆盖列表页 URL，
        # 导致封面下载全部失败。
        self.assertEqual(
            parse_detail_payload(self.payload, site_domain="example.net").thumbnail,
            "https://t.example.net/galleries/9900003/thumb.webp",
        )

    def test_original_title_and_key(self):
        detail = parse_detail_payload(self.payload)
        self.assertIn("サンプル", detail.title_original)
        self.assertEqual(detail.title_pretty, 'Sample Anthology "Sample" Vol.3')
        self.assertTrue(detail.original_key)
        self.assertNotIn("dl", detail.original_key)


class DetailMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("detail")
        self.db_path = self.tmp / "test.sqlite3"
        db.init_db(self.db_path)
        page = parse_listing(
            (SAMPLES / "listing_new_demo.html").read_text(encoding="utf-8"),
            "https://example.net/group/samplecircle/",
        )
        self.items = page.items
        db.upsert_items(self.items, source_url="demo", path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ids_needing_detail(self):
        ids = [item.gallery_id for item in self.items]
        self.assertEqual(sorted(db.ids_needing_detail(ids, self.db_path)), sorted(ids))
        detail = parse_detail_payload(
            json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8"))
        )
        db.apply_details([detail], self.db_path)
        self.assertNotIn(910001, db.ids_needing_detail(ids, self.db_path))
        self.assertIn(910004, db.ids_needing_detail(ids, self.db_path))

    def test_original_key_merges_variants_precisely(self):
        payloads = [
            json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8")),
            json.loads((SAMPLES / "detail_910004.json").read_text(encoding="utf-8")),
        ]
        db.apply_details([parse_detail_payload(payload) for payload in payloads], self.db_path)

        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["raw_total"], 4)
        self.assertEqual(data["total"], 2, "Vol.3 的三个语言版本应合并，Vol.2 独立")

        vol3 = next(item for item in data["items"] if item["title_original"])
        self.assertEqual(vol3["variant_count"], 3)
        self.assertEqual(vol3["title"], 'Sample Anthology "Sample" Vol.3')
        self.assertEqual(vol3["num_pages"], 32)
        self.assertEqual(vol3["num_favorites"], 1203)

    def test_sort_by_favorites_and_pages(self):
        payloads = [
            json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8")),
            json.loads((SAMPLES / "detail_910004.json").read_text(encoding="utf-8")),
        ]
        db.apply_details([parse_detail_payload(payload) for payload in payloads], self.db_path)
        data = db.query_works(path=self.db_path, sort="pages", per_page=50)
        pages = [item["num_pages"] or 0 for item in data["items"]]
        self.assertEqual(pages, sorted(pages, reverse=True))
        data = db.query_works(path=self.db_path, sort="favorites", per_page=50)
        favorites = [item["num_favorites"] or 0 for item in data["items"]]
        self.assertEqual(favorites, sorted(favorites, reverse=True))

    def test_stats_reports_detail_count(self):
        detail = parse_detail_payload(
            json.loads((SAMPLES / "detail_910001.json").read_text(encoding="utf-8"))
        )
        db.apply_details([detail], self.db_path)
        info = db.stats(self.db_path)
        self.assertEqual(info["entries"], 4)
        self.assertEqual(info["details"], 1)


if __name__ == "__main__":
    unittest.main()