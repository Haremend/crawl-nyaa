"""SQLite 存取层测试：入库去重、同作品合并、语言过滤、统计。"""
import shutil
import unittest
import uuid
from pathlib import Path

from app import db
from app.parser import parse_listing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)

SAMPLE = PROJECT_ROOT / "samples" / "listing_demo.html"
BASE_URL = "https://example.net/group/samplecircle/"


def make_temp_dir(prefix: str) -> Path:
    # 不用 tempfile.TemporaryDirectory：它创建 0o700 目录，
    # 在受限环境下会连当前进程都无法访问，这里用默认权限的普通目录。
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("db")
        self.db_path = self.tmp / "test.sqlite3"
        page = parse_listing(SAMPLE.read_text(encoding="utf-8"), BASE_URL)
        self.items = page.items
        db.init_db(self.db_path)
        self.first = db.upsert_items(self.items, source_url=BASE_URL, path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_insert(self):
        self.assertEqual(self.first["seen"], 4)
        self.assertEqual(self.first["new"], 4)
        self.assertEqual(self.first["dup"], 0)

    def test_reinsert_counts_as_duplicate(self):
        again = db.upsert_items(self.items, source_url=BASE_URL, path=self.db_path)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["dup"], 4)
        self.assertEqual(db.stats(self.db_path)["entries"], 4)

    def test_dedupe_merges_language_variants(self):
        data = db.query_works(path=self.db_path, dedupe_title=True, per_page=50)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["raw_total"], 4)
        alpha = next(item for item in data["items"] if item["title"] == "Sample Work Alpha")
        self.assertEqual(alpha["variant_count"], 2)
        self.assertEqual({v["language"] for v in alpha["variants"]}, {"english", "japanese"})
        self.assertEqual(set(alpha["language_keys"]), {"english", "japanese"})
        # 选版优先级：日语 > 中文 > 英文，故主版本为日语 900002
        self.assertEqual(alpha["url"], "https://example.net/g/900002/")
        self.assertEqual(alpha["primary_language"], "japanese")

    def test_dedupe_disabled_keeps_all_entries(self):
        data = db.query_works(path=self.db_path, dedupe_title=False, per_page=50)
        self.assertEqual(data["total"], 4)
        self.assertTrue(all(item["variant_count"] == 1 for item in data["items"]))

    def test_language_filter(self):
        data = db.query_works(path=self.db_path, languages=["chinese"], per_page=50)
        self.assertEqual(
            sorted(item["title"] for item in data["items"]),
            ["Sample Work Beta [Chinese]", "Sample Work Gamma"],
        )

    def test_language_stats_counts_works(self):
        data = db.query_works(path=self.db_path, per_page=50)
        stats = {entry["key"]: entry["count"] for entry in data["language_stats"]}
        self.assertEqual(stats["english"], 2)   # alpha(合并后) + gamma
        self.assertEqual(stats["chinese"], 2)
        self.assertEqual(stats["japanese"], 1)

    def test_search(self):
        self.assertEqual(db.query_works(path=self.db_path, q="Beta", per_page=50)["total"], 1)
        self.assertEqual(db.query_works(path=self.db_path, q="samplecircle", per_page=50)["total"], 3)

    def test_stats(self):
        info = db.stats(self.db_path)
        self.assertEqual(info["entries"], 4)
        self.assertEqual(info["works"], 3)
        self.assertEqual(info["duplicates"], 1)

    def test_reset(self):
        removed = db.reset_all(self.db_path)
        self.assertEqual(removed["galleries"], 4)
        self.assertEqual(db.stats(self.db_path)["entries"], 0)


if __name__ == "__main__":
    unittest.main()