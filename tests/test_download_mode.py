""".env 解析 + 立即下载两种模式（cache / stream）测试。"""
import io
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import config, db
from app.envfile import load_env, parse_env_text
from app.server import ApiServer, Handler

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


class EnvFileTests(unittest.TestCase):
    def test_parse_basic(self):
        text = "\n".join([
            "# 注释",
            "",
            "A=1",
            "B = two words ",
            "C=value # trailing comment",
            'D="quoted value"',
            "E='single '",
            "F=",
            "export G=7",
            "BROKEN_LINE",
        ])
        parsed = parse_env_text(text)
        self.assertEqual(parsed["A"], "1")
        self.assertEqual(parsed["B"], "two words")
        self.assertEqual(parsed["C"], "value")
        self.assertEqual(parsed["D"], "quoted value")
        self.assertEqual(parsed["E"], "single ")
        self.assertEqual(parsed["F"], "")
        self.assertEqual(parsed["G"], "7")
        self.assertNotIn("BROKEN_LINE", parsed)

    def test_load_env_does_not_override_existing(self):
        import os
        tmp = make_temp_dir("envfile")
        try:
            env_file = tmp / ".env"
            env_file.write_text("CLAW_TEST_NEW=fromfile\nCLAW_TEST_EXIST=fromfile\n", encoding="utf-8")
            os.environ["CLAW_TEST_EXIST"] = "fromenv"
            os.environ.pop("CLAW_TEST_NEW", None)
            applied = load_env(env_file)
            self.assertEqual(os.environ["CLAW_TEST_NEW"], "fromfile")
            self.assertEqual(os.environ["CLAW_TEST_EXIST"], "fromenv", "已存在的环境变量优先")
            self.assertNotIn("CLAW_TEST_EXIST", applied)
        finally:
            import os
            os.environ.pop("CLAW_TEST_NEW", None)
            os.environ.pop("CLAW_TEST_EXIST", None)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_file_is_noop(self):
        self.assertEqual(load_env(PROJECT_ROOT / "definitely-not-exist.env"), {})


class DownloadModeConfigTests(unittest.TestCase):
    def test_config_is_one_of_two_modes(self):
        self.assertIn(config.DOWNLOAD_MODE, ("cache", "stream"))

    def test_downloads_dir_is_reused_as_cache(self):
        # cache 模式复用一键下载的输出目录，避免重复占用空间
        self.assertTrue(str(config.DOWNLOADS_DIR).endswith("download"))


class _FakeResponse:
    """模拟上游流式响应。"""

    def __init__(self, payload: bytes, content_type: str = "application/zip"):
        self._buffer = io.BytesIO(payload)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        self.closed = False

        class _Headers:
            def __init__(self, data):
                self._data = data

            def get_content_type(self):
                return self._data.get("Content-Type")

            def get(self, key, default=None):
                return self._data.get(key, default)

        self.headers = _Headers(self.headers)

    def read(self, size=-1):
        return self._buffer.read(size)

    def close(self):
        self.closed = True


class DownloadCacheModeTests(unittest.TestCase):
    """cache 模式：先落盘再转发；重复请求直接命中缓存。"""

    def setUp(self):
        self.tmp = make_temp_dir("cachemode")
        self.db_path = self.tmp / "t.sqlite3"
        self.cache_dir = self.tmp / "download"
        db.init_db(self.db_path)
        db.upsert_items(
            [{"gallery_id": 4242, "url": "https://example.net/g/4242/", "title": "Cached Work",
              "languages": ["japanese"]}],
            source_url="https://example.net/group/x/", path=self.db_path,
        )
        db.set_setting("site_origin", "https://example.net", self.db_path)

        self._orig_mode = config.DOWNLOAD_MODE
        self._orig_dir = config.DOWNLOADS_DIR
        config.DOWNLOAD_MODE = "cache"
        config.DOWNLOADS_DIR = self.cache_dir

        self.payload = b"PK\x03\x04" + b"x" * 200000
        self.calls = {"upstream": 0}

        import app.server as server_module
        import app.upstream as upstream_module
        self.server_module = server_module
        self.upstream_module = upstream_module
        self._orig_get_url = upstream_module.UpstreamClient.get_download_url
        self._orig_open_stream = server_module.Fetcher.open_stream

        def fake_get_download_url(self_client, gallery_id, fmt="zip"):
            self.calls["upstream"] += 1
            return {"url": "https://cdn.example.net/download/1?sign=abc", "expires_at": 1}

        def fake_open_stream(self_fetcher, url, referer=None):
            return _FakeResponse(self.payload)

        upstream_module.UpstreamClient.get_download_url = fake_get_download_url
        server_module.Fetcher.open_stream = fake_open_stream

        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.upstream_module.UpstreamClient.get_download_url = self._orig_get_url
        self.server_module.Fetcher.open_stream = self._orig_open_stream
        config.DOWNLOAD_MODE = self._orig_mode
        config.DOWNLOADS_DIR = self._orig_dir
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _download(self):
        url = (f"{self.base}/api/download/file?gallery_id=4242&format=zip&apikey=test-key")
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read(), response.headers

    def test_cache_mode_writes_file_then_serves(self):
        status, body, headers = self._download()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload, "返回内容应与上游一致")
        self.assertEqual(self.calls["upstream"], 1, "应只请求上游一次")

        files = list(self.cache_dir.glob("4242_*"))
        self.assertEqual(len(files), 1, "文件应落到服务器磁盘")
        self.assertEqual(files[0].stat().st_size, len(self.payload))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertTrue(headers.get("Content-Disposition", "").startswith("attachment"))

    def test_second_request_hits_cache(self):
        self._download()
        status, body, _headers = self._download()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload)
        self.assertEqual(self.calls["upstream"], 1, "第二次应命中缓存，不再请求上游")

    def test_no_part_file_left(self):
        self._download()
        leftovers = list(self.cache_dir.glob("*.part"))
        self.assertEqual(leftovers, [], "临时文件应已原子改名，不留 .part")


class DownloadStreamModeTests(unittest.TestCase):
    """stream 模式：不在服务器落盘。"""

    def setUp(self):
        self.tmp = make_temp_dir("streammode")
        self.db_path = self.tmp / "t.sqlite3"
        self.cache_dir = self.tmp / "download"
        db.init_db(self.db_path)
        db.upsert_items(
            [{"gallery_id": 5151, "url": "https://example.net/g/5151/", "title": "Stream Work",
              "languages": ["japanese"]}],
            source_url="https://example.net/group/x/", path=self.db_path,
        )
        db.set_setting("site_origin", "https://example.net", self.db_path)

        self._orig_mode = config.DOWNLOAD_MODE
        self._orig_dir = config.DOWNLOADS_DIR
        config.DOWNLOAD_MODE = "stream"
        config.DOWNLOADS_DIR = self.cache_dir

        self.payload = b"stream-bytes" * 1000
        import app.server as server_module
        import app.upstream as upstream_module
        self.server_module = server_module
        self.upstream_module = upstream_module
        self._orig_get_url = upstream_module.UpstreamClient.get_download_url
        self._orig_open_stream = server_module.Fetcher.open_stream
        upstream_module.UpstreamClient.get_download_url = (
            lambda self_client, gallery_id, fmt="zip": {"url": "https://cdn.example.net/d?sign=1"})
        server_module.Fetcher.open_stream = (
            lambda self_fetcher, url, referer=None: _FakeResponse(self.payload))

        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.upstream_module.UpstreamClient.get_download_url = self._orig_get_url
        self.server_module.Fetcher.open_stream = self._orig_open_stream
        config.DOWNLOAD_MODE = self._orig_mode
        config.DOWNLOADS_DIR = self._orig_dir
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stream_mode_does_not_write_disk(self):
        url = f"{self.base}/api/download/file?gallery_id=5151&format=zip&apikey=k"
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read()
        self.assertEqual(body, self.payload)
        self.assertFalse(self.cache_dir.exists(), "stream 模式不应在服务器落盘")


if __name__ == "__main__":
    unittest.main()