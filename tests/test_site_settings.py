"""站点域名设置 + 抓取详情/封面路径的回归测试。"""
import json
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import config, db
from app.server import ApiServer, Handler, normalize_origin

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


class NormalizeOriginTests(unittest.TestCase):
    def test_accepts_common_forms(self):
        for raw, expected in (
            ("example.com", "https://example.com"),
            ("Example.com", "https://example.com"),
            ("https://example.com/", "https://example.com"),
            ("HTTPS://Example.COM/", "https://example.com"),
            ("http://example.com:8080/x", "http://example.com:8080"),
            ("localhost:9000", "https://localhost:9000"),
        ):
            self.assertEqual(normalize_origin(raw), expected, raw)

    def test_rejects_invalid(self):
        for raw in ("", "   ", "not a domain", "https://"):
            self.assertEqual(normalize_origin(raw), "", raw)


class SiteSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("siteset")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setting_takes_priority_over_scrape_url(self):
        # 先有一条抓取记录（推导来源）
        db.create_run("https://from-scrape.example.net/group/x/", self.db_path)
        db.upsert_items(
            [{"gallery_id": 1, "url": "https://from-scrape.example.net/g/1/",
              "title": "x", "languages": ["japanese"]}],
            source_url="https://from-scrape.example.net/group/x/", path=self.db_path,
        )
        self.assertEqual(db.get_site_origin(self.db_path), "https://from-scrape.example.net")

        # 前端设置的域名应优先
        db.set_setting("site_origin", "https://from-gate.example.com", self.db_path)
        self.assertEqual(db.get_site_origin(self.db_path), "https://from-gate.example.com")


class ScrapeDetailCoverPathTests(unittest.TestCase):
    """回归：抓取时详情补全 / 封面下载曾因 site_domain 未定义而 NameError。"""

    def setUp(self):
        self.tmp = make_temp_dir("detailpath")
        self.db_path = self.tmp / "t.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scrape_with_enrich_and_covers_does_not_raise(self):
        from app import covers as covers_module
        from app import scrape_service as service_module
        from app.parser import GalleryItem
        from app.scraper import PageResult
        from app.scrape_service import ScrapeService

        original_iter = service_module.iter_pages
        original_fetch_detail = service_module.fetch_detail
        original_download_cover = covers_module.download_cover
        calls = {"detail": 0, "cover": 0}

        def fake_iter_pages(url, **_kwargs):
            yield PageResult(
                page_no=1, url=url,
                items=[GalleryItem(gallery_id=77, url="https://example.net/g/77/",
                                   title="x", languages=["japanese"], thumbnail="https://t.example.net/galleries/7/1t.jpg")],
                last_page=1, has_next=False,
            )

        def fake_fetch_detail(gallery_id, fetcher=None, site_domain=""):
            calls["detail"] += 1
            calls["site_domain"] = site_domain
            return None                      # 返回 None 表示失败，避免写库

        def fake_download_cover(gallery_id, url, *, cover_dir, fetcher=None, site_domain="", **kwargs):
            calls["cover"] += 1
            calls["cover_site_domain"] = site_domain
            return None

        service_module.iter_pages = fake_iter_pages
        service_module.fetch_detail = fake_fetch_detail
        covers_module.download_cover = fake_download_cover
        try:
            service = ScrapeService(self.db_path)
            config.set_proxy_enabled(False)
            service.start("https://example.net/group/x/", max_pages=1, enrich=True, covers=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and service.snapshot()["running"]:
                time.sleep(0.05)
            snapshot = service.snapshot()
        finally:
            service_module.iter_pages = original_iter
            service_module.fetch_detail = original_fetch_detail
            covers_module.download_cover = original_download_cover
            config.set_proxy_enabled(True)

        self.assertEqual(snapshot["status"], "done",
                         f"抓取不应失败：{snapshot.get('error')}")
        self.assertEqual(calls["detail"], 1, "应该走过详情补全")
        self.assertEqual(calls["cover"], 1, "应该走过封面下载")
        self.assertEqual(calls["site_domain"], "example.net", "详情补全应拿到站点域名")
        self.assertEqual(calls["cover_site_domain"], "example.net", "封面下载应拿到站点域名")


class SiteEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("siteapi")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_set_and_expose_site_origin(self):
        status, data = self.call("/api/settings/site", "POST", {"origin": "example.com"})
        self.assertEqual(status, 200)
        self.assertEqual(data["site_origin"], "https://example.com")
        self.assertEqual(db.get_setting("site_origin", None, self.db_path), "https://example.com")

        _status, meta = self.call("/api/meta")
        self.assertEqual(meta["site_origin"], "https://example.com")

    def test_rejects_bad_origin(self):
        status, data = self.call("/api/settings/site", "POST", {"origin": "not a domain"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()