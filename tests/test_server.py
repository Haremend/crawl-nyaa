"""HTTP 服务冒烟测试：只访问本地回环地址，不访问外网。"""
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

from app import db
from app.parser import parse_listing
from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)

SAMPLE = PROJECT_ROOT / "samples" / "listing_demo.html"
BASE_URL = "https://example.net/group/samplecircle/"


def make_temp_dir(prefix: str) -> Path:
    # 不用 tempfile.TemporaryDirectory：它创建 0o700 目录，
    # 在受限环境下会连当前进程都无法访问。
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = make_temp_dir("server")
        cls.db_path = cls.tmp / "server.sqlite3"
        db.init_db(cls.db_path)
        page = parse_listing(SAMPLE.read_text(encoding="utf-8"), BASE_URL)
        db.upsert_items(page.items, source_url=BASE_URL, path=cls.db_path)
        cls.server = ApiServer(("127.0.0.1", 0), Handler, db_path=cls.db_path)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------- 工具 ----------
    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def get_json(self, path):
        status, body = self.get(path)
        return status, json.loads(body.decode("utf-8"))

    def post_json(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    # ---------- 测试 ----------
    def test_index_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("claw-gallery", body.decode("utf-8"))

    def test_static_assets(self):
        status, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("loadWorks", body.decode("utf-8"))
        status, body = self.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn(".card", body.decode("utf-8"))

    def test_meta(self):
        status, data = self.get_json("/api/meta")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["proxy"], "http://127.0.0.1:10808")
        self.assertEqual(data["stats"]["entries"], 4)

    def test_works_query(self):
        status, data = self.get_json("/api/works?dedupe=1&per_page=50")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 3)
        self.assertTrue(any(item["variant_count"] == 2 for item in data["items"]))
        self.assertTrue(all(item["thumb"].startswith("/api/thumb?u=")
                            for item in data["items"] if item["thumbnail"]))

    def test_works_language_filter(self):
        _status, data = self.get_json("/api/works?lang=chinese&dedupe=1&per_page=50")
        self.assertEqual(data["total"], 2)

    def test_thumb_accepts_any_http_url(self):
        # 不再按域名拒绝；非 http(s) 才拒绝
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/thumb?u=" + quote("javascript:alert(1)", safe=""))
        self.assertEqual(ctx.exception.code, 400)

    def test_scrape_accepts_foreign_url(self):
        # 域名检查已移除；只测 URL 校验不启动真实抓取（避免干扰后续测试）
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_json("/api/scrape", {"url": "not-a-url"})
        self.assertEqual(ctx.exception.code, 400)

    def test_scrape_rejects_detail_page(self):
        import time
        for _ in range(20):
            _s, d = self.get_json("/api/scrape/status")
            if not d.get("running"):
                break
            self.post_json("/api/scrape/cancel", {})
            time.sleep(0.2)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_json("/api/scrape", {"url": "https://example.net/g/123/"})
        self.assertEqual(ctx.exception.code, 400)

    def test_scrape_status_idle(self):
        # 等待可能的前一个测试任务结束
        import time
        for _ in range(20):
            _status, data = self.get_json("/api/scrape/status")
            if not data.get("running"):
                break
            time.sleep(0.2)
        self.assertTrue(data["ok"])
        self.assertFalse(data["running"])

    def test_z_reset(self):
        _status, data = self.post_json("/api/reset", {})
        self.assertTrue(data["ok"])
        self.assertEqual(data["removed"]["galleries"], 4)
        _status, meta = self.get_json("/api/meta")
        self.assertEqual(meta["stats"]["entries"], 0)


if __name__ == "__main__":
    unittest.main()