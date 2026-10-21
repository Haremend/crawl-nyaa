"""下载文件名净化 + 下载目标过滤 + 文件转发接口测试。"""
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import db
from app.filenames import download_filename, sanitize_filename
from app.parser import GalleryItem
from app.server import ApiServer, Handler, _content_disposition

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)

# Windows 非法字符（测过会被替换）
ILLEGAL = '<>:"/\\|?*'


from app import config as _lock_cfg, supabase_client as _lock_sc
_lock_cfg.STORAGE_BACKEND = "local"
_lock_cfg.SUPABASE_PROJECT_REF = ""
_lock_cfg.SUPABASE_TOKEN = ""
_lock_sc.set_transport(None)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class SanitizeTests(unittest.TestCase):
    def test_removes_illegal_characters(self):
        cleaned = sanitize_filename("a" + ILLEGAL + "b")
        for ch in ILLEGAL:
            self.assertNotIn(ch, cleaned)
        self.assertTrue(cleaned.startswith("a"))

    def test_strips_trailing_dots_and_spaces(self):
        self.assertEqual(sanitize_filename("name..."), "name")
        self.assertEqual(sanitize_filename("name   "), "name")

    def test_removes_control_characters(self):
        cleaned = sanitize_filename("a\nb\tc\x00d")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\t", cleaned)
        self.assertNotIn("\x00", cleaned)

    def test_avoids_windows_reserved_names(self):
        for name in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            self.assertNotEqual(sanitize_filename(name).upper(), name.upper())

    def test_empty_gets_fallback(self):
        self.assertEqual(sanitize_filename(""), "untitled")
        self.assertEqual(sanitize_filename("..."), "untitled")
        self.assertEqual(sanitize_filename("", fallback=""), "")

    def test_length_is_capped(self):
        self.assertLessEqual(len(sanitize_filename("x" * 500, max_length=50)), 50)


class DownloadFilenameTests(unittest.TestCase):
    def test_format_is_id_underscore_title(self):
        self.assertEqual(download_filename(682380, "Some Title", "zip"), "682380_Some Title.zip")

    def test_empty_title_falls_back_to_id_only(self):
        self.assertEqual(download_filename(123, "", "zip"), "123.zip")
        self.assertEqual(download_filename(123, "   ", "zip"), "123.zip")

    def test_illegal_chars_are_cleaned(self):
        name = download_filename(1, "a/b:c*d", "zip")
        for ch in ILLEGAL:
            self.assertNotIn(ch, name)
        self.assertTrue(name.startswith("1_"))

    def test_long_title_keeps_id_prefix_and_is_capped(self):
        name = download_filename(999, "x" * 500, "zip")
        self.assertTrue(name.startswith("999_"))
        self.assertLess(len(name), 255, "文件名不能超过文件系统上限")
        self.assertTrue(name.endswith(".zip"))

    def test_unicode_title_kept(self):
        name = download_filename(5, "日本語タイトル", "cbz")
        self.assertEqual(name, "5_日本語タイトル.cbz")

    def test_content_disposition_has_utf8_form(self):
        header = _content_disposition("5_日本語タイトル.cbz")
        self.assertIn("attachment;", header)
        self.assertIn("filename*=UTF-8''", header)


class SkipDownloadedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("skipdone")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        db.upsert_items(
            [
                GalleryItem(gallery_id=1001, url="https://example.net/g/1001/", title="A",
                            languages=["japanese"], title_key="a"),
                GalleryItem(gallery_id=1002, url="https://example.net/g/1002/", title="B",
                            languages=["japanese"], title_key="b"),
            ],
            source_url="https://example.net/group/x/", path=self.db_path,
        )
        db.set_want_download([1001, 1002], True, self.db_path)

    def test_want_targets_skip_downloaded(self):
        self._seed()
        self.assertEqual(len(db.list_want_download_targets(self.db_path)), 2)
        db.set_downloaded([1001], True, self.db_path)
        targets = db.list_want_download_targets(self.db_path)
        ids = [t["gallery_id"] for t in targets]
        self.assertEqual(ids, [1002], "已下载的作品不应再出现在待下载队列")

    def test_batch_targets_skip_downloaded(self):
        run_id = db.create_run("https://example.net/group/x/", self.db_path)
        db.upsert_items(
            [GalleryItem(gallery_id=2001, url="https://example.net/g/2001/", title="C",
                         languages=["japanese"], title_key="c")],
            source_url="https://example.net/group/x/", batch_id=run_id, path=self.db_path,
        )
        self.assertEqual(len(db.list_batch_targets(run_id, self.db_path)), 1)
        db.set_downloaded([2001], True, self.db_path)
        self.assertEqual(db.list_batch_targets(run_id, self.db_path), [],
                         "整批保存时也应跳过已下载")


class DownloadFileEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("dlfile")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_api_key(self):
        # 没有 Key 时应返回错误页而不是让浏览器停在 JSON 上
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/download/file?gallery_id=1&format=zip", timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        body = ctx.exception.read().decode("utf-8", errors="replace")
        self.assertIn("text/html", ctx.exception.headers.get("Content-Type", ""))
        self.assertIn("API Key", body)


class RedownloadModeTests(unittest.TestCase):
    """「重新下载」开关：False 按数据库标记跳过，True 按文件名跳过。"""

    def setUp(self):
        self.tmp = make_temp_dir("redownload")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        db.upsert_items(
            [GalleryItem(gallery_id=3001, url="https://example.net/g/3001/", title="A",
                         languages=["japanese"], title_key="a")],
            source_url="https://example.net/group/x/", path=self.db_path,
        )
        db.set_want_download([3001], True, self.db_path)
        db.set_downloaded([3001], True, self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_skips_db_downloaded(self):
        self.assertEqual(db.list_want_download_targets(self.db_path), [],
                         "默认（不重新下载）应跳过数据库里已标记已下载的作品")

    def test_redownload_includes_db_downloaded(self):
        targets = db.list_want_download_targets(self.db_path, skip_downloaded=False)
        self.assertEqual([t["gallery_id"] for t in targets], [3001],
                         "重新下载模式应忽略数据库标记，把作品重新纳入队列")

    def test_batch_targets_respect_flag(self):
        run_id = db.create_run("https://example.net/group/x/", self.db_path)
        db.upsert_items(
            [GalleryItem(gallery_id=3002, url="https://example.net/g/3002/", title="B",
                         languages=["japanese"], title_key="b")],
            source_url="https://example.net/group/x/", batch_id=run_id, path=self.db_path,
        )
        db.set_downloaded([3002], True, self.db_path)
        self.assertEqual(db.list_batch_targets(run_id, self.db_path), [])
        self.assertEqual(len(db.list_batch_targets(run_id, self.db_path, skip_downloaded=False)), 1)

    def test_service_accepts_redownload_flag(self):
        import inspect
        from app.downloader import DownloadService
        params = inspect.signature(DownloadService.start).parameters
        self.assertIn("redownload", params, "下载服务应支持 redownload 参数")


if __name__ == "__main__":
    unittest.main()