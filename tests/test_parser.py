"""解析器单元测试（使用 samples/listing_demo.html 离线样本，不访问外网）。"""
import unittest
from pathlib import Path

from app.parser import clean_text, normalize_title_key, parse_listing
from app.scraper import page_url

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "listing_demo.html"
BASE_URL = "https://example.net/group/samplecircle/"


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = parse_listing(SAMPLE.read_text(encoding="utf-8"), BASE_URL)

    def test_items_parsed(self):
        self.assertEqual(
            [item.gallery_id for item in self.page.items],
            [900001, 900002, 900003, 900004],
        )

    def test_item_fields(self):
        item = self.page.items[0]
        self.assertEqual(item.title, "Sample Work Alpha")
        self.assertEqual(item.url, "https://example.net/g/900001/")
        self.assertEqual(item.thumbnail, "https://t3.example.net/galleries/3000001/1t.jpg")
        self.assertEqual(item.media_id, 3000001)
        self.assertEqual(item.languages, ["english"])
        self.assertIn("samplecircle", item.artists)
        self.assertIn("samplecircle", item.groups)
        self.assertIn("sample parody", item.parodies)
        self.assertIn("sample chan", item.characters)
        self.assertIn("sole female", item.tags)
        self.assertIn("doujinshi", item.categories)

    def test_title_key_merges_language_variants(self):
        alpha_en, alpha_jp = self.page.items[0], self.page.items[1]
        self.assertEqual(alpha_en.title_key, alpha_jp.title_key)
        self.assertNotEqual(alpha_en.title_key, self.page.items[2].title_key)

    def test_multi_language_item(self):
        self.assertEqual(set(self.page.items[3].languages), {"english", "chinese"})

    def test_pagination_first_page(self):
        self.assertEqual(self.page.last_page, 2)
        self.assertTrue(self.page.has_next)
        self.assertEqual(self.page.current_page, 1)

    def test_pagination_second_page(self):
        # 模拟末页 HTML（去掉 Next / Last 链接）后再以 ?page=2 解析
        html = SAMPLE.read_text(encoding="utf-8")
        html = html.replace('<a class="next" href="?page=2">Next &rsaquo;</a>', "")
        html = html.replace('<a class="last" href="?page=2">Last &raquo;</a>', "")
        page = parse_listing(html, BASE_URL + "?page=2")
        self.assertEqual(page.current_page, 2)
        self.assertFalse(page.has_next)

    def test_normalize_title_key(self):
        self.assertEqual(normalize_title_key("Sample Work Beta [Chinese]"), "sampleworkbeta")
        self.assertEqual(normalize_title_key(" sample  WORK beta "), "sampleworkbeta")

    def test_clean_text(self):
        self.assertEqual(clean_text("  a\u200b  b\n c "), "a b c")


class PageUrlTests(unittest.TestCase):
    def test_first_page_drops_page_param(self):
        self.assertEqual(
            page_url("https://example.net/group/samplecircle/?page=5", 1),
            "https://example.net/group/samplecircle/",
        )

    def test_later_page_keeps_other_params(self):
        self.assertEqual(
            page_url("https://example.net/group/samplecircle/?sort=popular", 3),
            "https://example.net/group/samplecircle/?sort=popular&page=3",
        )


if __name__ == "__main__":
    unittest.main()