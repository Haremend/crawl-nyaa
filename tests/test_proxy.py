"""代理开关测试：运行时可切换、可持久化、并真正影响出网请求。"""
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import config, db
from app.scraper import Fetcher
from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class ProxyConfigTests(unittest.TestCase):
    def setUp(self):
        self.original = config.PROXY_ENABLED

    def tearDown(self):
        config.set_proxy_enabled(self.original)

    def test_toggle_affects_effective_proxy(self):
        self.assertTrue(config.PROXY, "默认应有代理地址")
        config.set_proxy_enabled(True)
        self.assertEqual(config.effective_proxy(), config.PROXY)
        config.set_proxy_enabled(False)
        self.assertEqual(config.effective_proxy(), "", "关闭后应为空字符串（直连）")

    def test_fetcher_follows_runtime_switch(self):
        config.set_proxy_enabled(True)
        self.assertEqual(Fetcher().proxy, config.PROXY)
        config.set_proxy_enabled(False)
        self.assertEqual(Fetcher().proxy, "", "关闭代理后新建的 Fetcher 不应再使用代理")

    def test_explicit_proxy_still_wins(self):
        config.set_proxy_enabled(False)
        self.assertEqual(Fetcher("http://127.0.0.1:9999").proxy, "http://127.0.0.1:9999",
                         "显式传入的代理不受开关影响")


class SettingsStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("settings")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setting_roundtrip(self):
        self.assertEqual(db.get_setting("proxy_enabled", "default", self.db_path), "default")
        db.set_setting("proxy_enabled", "0", self.db_path)
        self.assertEqual(db.get_setting("proxy_enabled", None, self.db_path), "0")
        db.set_setting("proxy_enabled", "1", self.db_path)
        self.assertEqual(db.get_setting("proxy_enabled", None, self.db_path), "1")


class ProxyEndpointTests(unittest.TestCase):
    def setUp(self):
        self.original = config.PROXY_ENABLED
        self.tmp = make_temp_dir("proxyapi")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        config.set_proxy_enabled(self.original)
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

    def test_meta_exposes_proxy_state(self):
        status, meta = self.call("/api/meta")
        self.assertEqual(status, 200)
        self.assertIn("proxy_enabled", meta)
        self.assertIn("proxy_address", meta)
        self.assertEqual(meta["proxy_address"], config.PROXY)

    def test_toggle_off_and_on(self):
        status, data = self.call("/api/settings/proxy", "POST", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["proxy_enabled"])
        self.assertFalse(config.PROXY_ENABLED)
        self.assertEqual(db.get_setting("proxy_enabled", None, self.db_path), "0")

        status, data = self.call("/api/settings/proxy", "POST", {"enabled": True})
        self.assertTrue(data["proxy_enabled"])
        self.assertEqual(db.get_setting("proxy_enabled", None, self.db_path), "1")

    def test_setting_restored_on_startup(self):
        db.set_setting("proxy_enabled", "0", self.db_path)
        config.set_proxy_enabled(True)          # 先设为开
        server2 = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        try:
            self.assertFalse(config.PROXY_ENABLED, "启动时应恢复数据库里保存的开关状态")
        finally:
            server2.server_close()


class ScrapeProxyTests(unittest.TestCase):
    """抓取任务必须跟随代理开关。

    历史 bug：ScrapeService 里写成 Fetcher(proxy=config.PROXY)，
    显式传了代理地址，于是页面关掉代理后抓取仍然走代理（报 407）。
    """

    def setUp(self):
        self.original = config.PROXY_ENABLED
        self.tmp = make_temp_dir("scrapeproxy")
        self.db_path = self.tmp / "t.sqlite3"

    def tearDown(self):
        config.set_proxy_enabled(self.original)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_fake_scrape(self):
        import time

        from app import service as service_module
        from app.parser import GalleryItem
        from app.scraper import PageResult
        from app.service import ScrapeService

        seen = {}

        class SpyFetcher(Fetcher):
            def __init__(self, proxy=None, **kwargs):
                seen["proxy_arg"] = proxy
                super().__init__(proxy, **kwargs)
                seen["proxy"] = self.proxy

        def fake_iter_pages(url, **_kwargs):
            yield PageResult(
                page_no=1,
                url=url,
                items=[GalleryItem(gallery_id=1, url="https://example.net/g/1/", title="x")],
                last_page=1,
                has_next=False,
            )

        original_fetcher = service_module.Fetcher
        original_iter = service_module.iter_pages
        service_module.Fetcher = SpyFetcher
        service_module.iter_pages = fake_iter_pages
        try:
            service = ScrapeService(self.db_path)
            service.start("https://example.net/group/x/", max_pages=1, enrich=False, covers=False)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and service.snapshot()["running"]:
                time.sleep(0.05)
        finally:
            service_module.Fetcher = original_fetcher
            service_module.iter_pages = original_iter
        return seen

    def test_scrape_uses_no_proxy_when_disabled(self):
        config.set_proxy_enabled(False)
        seen = self._run_fake_scrape()
        self.assertEqual(seen.get("proxy"), "", "代理关闭时抓取任务不应使用代理")
        self.assertIsNone(seen.get("proxy_arg"),
                          "抓取任务不应显式传 proxy，否则会绕过「开启代理」开关")

    def test_scrape_uses_proxy_when_enabled(self):
        config.set_proxy_enabled(True)
        seen = self._run_fake_scrape()
        self.assertEqual(seen.get("proxy"), config.PROXY)


if __name__ == "__main__":
    unittest.main()