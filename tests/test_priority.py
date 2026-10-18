"""选版优先级与无码过滤测试。

规则：无码 > 日语 > 中文 > 英文 > 其他；同级页数多者优先，再新者优先。
"""
import json
import shutil
import unittest
import uuid
from pathlib import Path

from app import db
from app.detail import parse_detail_payload
from app.parser import GalleryItem

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)
SAMPLES = PROJECT_ROOT / "samples"


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def detail_payload(gallery_id: int, *, language: str = "japanese", uncensored: bool = False,
                   num_pages: int = 24, num_favorites: int = 0, original: str = "Same Work"):
    tags = [{"type": "language", "name": language, "slug": language}]
    if language != "japanese":
        tags.append({"type": "language", "name": "translated", "slug": "translated"})
    if uncensored:
        tags.append({"type": "tag", "name": "uncensored", "slug": "uncensored"})
    return {
        "id": gallery_id,
        "media_id": str(gallery_id + 4000000),
        "title": {"english": f"Work #{gallery_id}", "japanese": original, "pretty": f"Work #{gallery_id}"},
        "thumbnail": {"path": f"galleries/{gallery_id}/thumb.webp"},
        "num_pages": num_pages,
        "num_favorites": num_favorites,
        "upload_date": 1750000000 + gallery_id,
        "tags": tags,
    }


class VariantPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("prio")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _group_of(data, gallery_id):
        """在查询结果里找到包含指定 gallery_id 的作品组。"""
        for item in data["items"]:
            if gallery_id in item["ids"]:
                return item
        raise AssertionError(f"未找到包含 {gallery_id} 的作品组")

    def _make_rows(self, ids):
        rows = [GalleryItem(gallery_id=gid, url=f"https://example.net/g/{gid}/",
                            title=f"Work #{gid}", languages=["unknown"])
                for gid in ids]
        db.upsert_items(rows, source_url="demo", path=self.db_path)

    def test_uncensored_beats_japanese(self):
        self._make_rows([100001, 100002])
        db.apply_details([
            parse_detail_payload(detail_payload(100001, language="english", uncensored=True,
                                                 num_pages=10, original="Same Work 1")),
            parse_detail_payload(detail_payload(100002, language="japanese", num_pages=40,
                                                 original="Same Work 1")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        item = data["items"][0]
        self.assertEqual(item["gallery_id"], 100001, "无码版本应压过日语版本")
        self.assertTrue(item["uncensored"])

    def test_language_priority_when_not_uncensored(self):
        self._make_rows([200001, 200002, 200003])
        db.apply_details([
            parse_detail_payload(detail_payload(200001, language="english", num_pages=10,
                                                 original="Same Work 2")),
            parse_detail_payload(detail_payload(200002, language="chinese", num_pages=10,
                                                 original="Same Work 2")),
            parse_detail_payload(detail_payload(200003, language="japanese", num_pages=10,
                                                 original="Same Work 2")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["gallery_id"], 200003, "日语优先于中文/英文")

    def test_more_pages_wins_among_same_language(self):
        self._make_rows([300001, 300002])
        db.apply_details([
            parse_detail_payload(detail_payload(300001, language="japanese", num_pages=20,
                                                 original="Same Work 3")),
            parse_detail_payload(detail_payload(300002, language="japanese", num_pages=35,
                                                 original="Same Work 3")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["gallery_id"], 300002, "同级语言里页数多者胜出")

    def test_newer_wins_on_tie(self):
        # 同作品、同语言、同页数 -> 新上传时间优先
        self._make_rows([400001, 400002])
        db.apply_details([
            parse_detail_payload(detail_payload(400001, language="japanese", num_pages=20,
                                                 original="Same Work 4")),
            parse_detail_payload(detail_payload(400002, language="japanese", num_pages=20,
                                                 original="Same Work 4")),
        ], self.db_path)
        # 手动让 400001 的上传时间更新 -> 400001 应胜出
        conn = db.connect(self.db_path)
        conn.execute("UPDATE galleries SET upload_date = '2026-01-01T00:00:00+00:00' WHERE gallery_id = 400001")
        conn.execute("UPDATE galleries SET upload_date = '2025-01-01T00:00:00+00:00' WHERE gallery_id = 400002")
        conn.commit(); conn.close()
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["items"][0]["gallery_id"], 400001, "同级同页数时新上传者胜出")

    def test_list_sort_newer_first_within_language(self):
        # 列表排序（非选版）：同语言 -> 新作排前面（不比页数）
        self._make_rows([500001, 500002])
        db.apply_details([
            parse_detail_payload(detail_payload(500001, language="japanese", num_pages=200,
                                                 original="Work Old")),
            parse_detail_payload(detail_payload(500002, language="japanese", num_pages=10,
                                                 original="Work New")),
        ], self.db_path)
        conn = db.connect(self.db_path)
        conn.execute("UPDATE galleries SET upload_date = '2024-01-01T00:00:00+00:00' WHERE gallery_id = 500001")
        conn.execute("UPDATE galleries SET upload_date = '2026-06-01T00:00:00+00:00' WHERE gallery_id = 500002")
        conn.commit(); conn.close()
        data = db.query_works(path=self.db_path, sort="priority", per_page=50)
        self.assertEqual(data["items"][0]["gallery_id"], 500002, "同语言列表排序：新作排前面（即使页数少）")

    def test_uncensored_filter(self):
        self._make_rows([500001, 500002])
        db.apply_details([
            parse_detail_payload(detail_payload(500001, language="japanese", uncensored=True,
                                                 original="Same Work 5")),
            parse_detail_payload(detail_payload(500002, language="japanese",
                                                 original="Same Work 6")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["total"], 2)
        uncensored_only = db.query_works(path=self.db_path, per_page=50, uncensored=True)
        self.assertEqual(uncensored_only["total"], 1)
        self.assertTrue(uncensored_only["items"][0]["uncensored"])

    def test_priority_sort_orders_checklist(self):
        self._make_rows([600001, 600002, 600003, 600004])
        db.apply_details([
            parse_detail_payload(detail_payload(600001, language="english", num_pages=10,
                                                 original="Work E")),
            parse_detail_payload(detail_payload(600002, language="chinese", num_pages=10,
                                                 original="Work C")),
            parse_detail_payload(detail_payload(600003, language="japanese", num_pages=10,
                                                 original="Work J")),
            parse_detail_payload(detail_payload(600004, language="japanese", uncensored=True,
                                                 num_pages=10, original="Work U")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, sort="priority", per_page=50)
        order = [item["gallery_id"] for item in data["items"]]
        self.assertEqual(order, [600004, 600003, 600002, 600001],
                         "优先级清单顺序：无码 > 日语 > 中文 > 英文")

    def test_language_filter_keeps_matched_main(self):
        # 过滤中文时，即便组里有日语版本，主版本也应是命中的中文版本
        self._make_rows([700001, 700002])
        db.apply_details([
            parse_detail_payload(detail_payload(700001, language="japanese", num_pages=30,
                                                 original="Same Work 7")),
            parse_detail_payload(detail_payload(700002, language="chinese", num_pages=10,
                                                 original="Same Work 7")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, languages=["chinese"], per_page=50)
        self.assertEqual(data["items"][0]["gallery_id"], 700002)
        self.assertEqual(data["items"][0]["primary_language"], "chinese")

    def test_favorite_is_recommended_variant_only(self):
        """收藏只作用于推荐版本；组内是否显示已收藏，也只认推荐版本。"""
        self._make_rows([800001, 800002])
        db.apply_details([
            parse_detail_payload(detail_payload(800001, language="english", num_pages=10,
                                                 original="Same Work 8")),
            parse_detail_payload(detail_payload(800002, language="japanese", num_pages=30,
                                                 original="Same Work 8")),
        ], self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        item = data["items"][0]
        # 推荐版本应是日语（语言优先级更高）
        self.assertEqual(item["gallery_id"], 800002)
        self.assertEqual(item["variant_count"], 2)

        # 只收藏「非推荐版本」时，组不应显示已收藏
        db.set_favorite([800001], True, self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertFalse(data["items"][0]["favorite"], "收藏非推荐版本时，组不应显示已收藏")

        # 改为收藏推荐版本（先清掉上一步的标记）
        db.set_favorite([800001], False, self.db_path)
        db.set_favorite([800002], True, self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        item = data["items"][0]
        self.assertTrue(item["favorite"], "收藏推荐版本后，组应显示已收藏")
        marks = {v["gallery_id"]: v["favorite"] for v in item["variants"]}
        self.assertTrue(marks[800002])
        self.assertFalse(marks[800001], "其它语言版本不应被一起收藏")

    def test_favorite_only_filter_follows_recommendation(self):
        self._make_rows([900001, 900002])
        db.apply_details([
            parse_detail_payload(detail_payload(900001, language="english", num_pages=10,
                                                 original="Same Work 9")),
            parse_detail_payload(detail_payload(900002, language="japanese", num_pages=30,
                                                 original="Same Work 9")),
        ], self.db_path)
        db.set_favorite([900002], True, self.db_path)
        fav = db.query_works(path=self.db_path, per_page=50, favorite_only=True)
        self.assertEqual(fav["total"], 1)
        self.assertEqual(fav["items"][0]["gallery_id"], 900002)

    def test_favorite_filter_excludes_when_main_changes(self):
        """推荐版本变化后（新版本优先级更高但未收藏），组不再显示已收藏。"""
        self._make_rows([950001, 950002, 950003])
        db.apply_details([
            parse_detail_payload(detail_payload(950001, language="chinese", num_pages=30,
                                                 original="Same Work 10")),
            parse_detail_payload(detail_payload(950002, language="japanese", num_pages=10,
                                                 original="Same Work 10")),
        ], self.db_path)
        # 先确认推荐版本是日语（950002）
        group = self._group_of(db.query_works(path=self.db_path, per_page=50), 950002)
        self.assertEqual(group["gallery_id"], 950002)
        db.set_favorite([950002], True, self.db_path)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50, favorite_only=True)["total"], 1)
        # 补上一个无码日语版本的详情（优先级最高）后，推荐版本变为它，
        # 而它并没有被收藏，所以这一组不再显示「已收藏」
        db.apply_details([
            parse_detail_payload(detail_payload(950003, language="japanese", uncensored=True,
                                                 num_pages=5, original="Same Work 10")),
        ], self.db_path)
        group = self._group_of(db.query_works(path=self.db_path, per_page=50), 950002)
        self.assertEqual(group["gallery_id"], 950003, "推荐版本应切换为无码日语版")
        self.assertFalse(group["favorite"], "推荐版本未被收藏，本组不应显示已收藏")


if __name__ == "__main__":
    unittest.main()