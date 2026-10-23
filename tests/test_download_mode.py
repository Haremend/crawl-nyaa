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


def anon_db_path(db_path: Path) -> Path:
    """未登录（匿名）请求的数据库路径：unlogin_user 用户库，与默认库同临时区。"""
    return db.user_db_path(config.UNLOGIN_USER, base_dir=db_path.parent)


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
        anon_db = anon_db_path(self.db_path)
        db.init_db(anon_db)
        db.upsert_items(
            [{"gallery_id": 4242, "url": "https://example.net/g/4242/", "title": "Cached Work",
              "languages": ["japanese"]}],
            source_url="https://example.net/group/x/", path=anon_db,
        )
        db.set_setting("site_origin", "https://example.net", anon_db)

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
        anon_db = anon_db_path(self.db_path)
        db.init_db(anon_db)
        db.upsert_items(
            [{"gallery_id": 5151, "url": "https://example.net/g/5151/", "title": "Stream Work",
              "languages": ["japanese"]}],
            source_url="https://example.net/group/x/", path=anon_db,
        )
        db.set_setting("site_origin", "https://example.net", anon_db)

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


class DownloadFilterTargetsTests(unittest.TestCase):
    """「下载当前筛选结果」的目标计算（filter_targets）。

    覆盖：语言筛选、同作品去重取推荐版本、跳过已下载、排除隐藏、
    dedupe=0 时含全部变体。全部走本地模式 + example.net 占位数据。
    """

    def setUp(self):
        self.tmp = make_temp_dir("filtertgt")
        self.db_path = self.tmp / "ft.sqlite3"
        db.init_db(self.db_path)
        self.anon_db = anon_db_path(self.db_path)
        db.init_db(self.anon_db)
        db.upsert_items([
            {"gallery_id": 1001, "url": "https://example.net/g/1001/",
             "title": "Alpha", "languages": ["japanese"]},
            {"gallery_id": 1002, "url": "https://example.net/g/1002/",
             "title": "Alpha", "languages": ["chinese"]},
            {"gallery_id": 2001, "url": "https://example.net/g/2001/",
             "title": "Beta", "languages": ["english"]},
            {"gallery_id": 3001, "url": "https://example.net/g/3001/",
             "title": "Gamma", "languages": ["japanese"]},
            {"gallery_id": 4001, "url": "https://example.net/g/4001/",
             "title": "Delta", "languages": ["japanese"]},
        ], source_url="https://example.net/group/x/", path=self.anon_db)
        db.set_downloaded([3001], True, self.anon_db)
        db.set_hidden([2001], True, self.anon_db)
        db.set_setting("site_origin", "https://example.net", self.anon_db)
        # 当前用户：匿名（unlogin_user）——filter_targets 用 anon_db
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.anon_db.parent

    def tearDown(self):
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _targets(self, **filters):
        from app.services import download_service
        base = {"sort": "recent", "dedupe_title": True, "page": 1, "per_page": 200}
        base.update(filters)
        return download_service.filter_targets(
            base, None, self.anon_db, cloud_mode=False, skip_downloaded=True)

    def test_language_filter_picks_recommended_variant(self):
        # lang=japanese dedupe=1：Alpha 组推荐日语 1001（非中文 1002）；
        # 2001 英语不命中语言；3001 已下载跳过；4001 命中
        ids = {t["gallery_id"] for t in self._targets(languages=["japanese"])}
        self.assertEqual(ids, {1001, 4001})

    def test_dedupe_off_includes_all_variants(self):
        # dedupe=0 不聚类：每个变体独立成条；隐藏/已下载仍排除
        ids = {t["gallery_id"] for t in self._targets(dedupe_title=False)}
        self.assertEqual(ids, {1001, 1002, 4001})

    def test_hidden_and_downloaded_excluded(self):
        # 不限语言、dedupe=1：2001 隐藏排除、3001 已下载跳过
        ids = {t["gallery_id"] for t in self._targets()}
        self.assertEqual(ids, {1001, 4001})
        self.assertNotIn(2001, ids)
        self.assertNotIn(3001, ids)


class DownloadFilterEndpointTests(unittest.TestCase):
    """POST /api/download/filter 端点烟测：入队 + 响应 shape（source=filter）。"""

    def setUp(self):
        self.tmp = make_temp_dir("filterep")
        self.db_path = self.tmp / "fe.sqlite3"
        self.cache_dir = self.tmp / "download"
        db.init_db(self.db_path)
        self.anon_db = anon_db_path(self.db_path)
        db.init_db(self.anon_db)
        db.upsert_items([
            {"gallery_id": 7001, "url": "https://example.net/g/7001/",
             "title": "Filter Work", "languages": ["japanese"]},
            {"gallery_id": 7002, "url": "https://example.net/g/7002/",
             "title": "Other", "languages": ["english"]},
        ], source_url="https://example.net/group/x/", path=self.anon_db)
        db.set_setting("site_origin", "https://example.net", self.anon_db)

        self._orig_mode = config.DOWNLOAD_MODE
        self._orig_dir = config.DOWNLOADS_DIR
        config.DOWNLOAD_MODE = "stream"
        config.DOWNLOADS_DIR = self.cache_dir
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.anon_db.parent

        import app.server as server_module
        import app.upstream as upstream_module
        self.server_module = server_module
        self.upstream_module = upstream_module
        self._orig_get_user = upstream_module.UpstreamClient.get_user
        self._orig_get_url = upstream_module.UpstreamClient.get_download_url
        self._orig_open_stream = server_module.Fetcher.open_stream
        # validate_key 走 get_user：patch 成空 dict 视为校验通过
        upstream_module.UpstreamClient.get_user = lambda self_c: {}
        upstream_module.UpstreamClient.get_download_url = (
            lambda self_c, gid, fmt="zip": {"url": "https://cdn.example.net/d?sign=1"})
        server_module.Fetcher.open_stream = (
            lambda self_f, url, referer=None: _FakeResponse(b"filter-bytes"))

        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.server.download_queue.start()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.upstream_module.UpstreamClient.get_user = self._orig_get_user
        self.upstream_module.UpstreamClient.get_download_url = self._orig_get_url
        self.server_module.Fetcher.open_stream = self._orig_open_stream
        config.DOWNLOAD_MODE = self._orig_mode
        config.DOWNLOADS_DIR = self._orig_dir
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        self.server.download_queue.stop()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filter_endpoint_enqueues_lang_filtered_targets(self):
        body = json.dumps({
            "filters": {"lang": ["japanese"], "dedupe": "1", "sort": "recent"},
            "format": "zip",
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/api/download/filter", data=body,
            headers={"Content-Type": "application/json", "X-API-Key": "k"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 1, "只日语 7001 命中（7002 英语排除）")
        self.assertEqual(data["queued"], 1)
        self.assertIn("queue", data)
        # source=filter 任务进队（job 行的 source 字段）
        jobs = data["queue"].get("jobs", [])
        self.assertTrue(any(str(j.get("source") or "") == "filter" for j in jobs))

    def test_filter_endpoint_empty_returns_400(self):
        body = json.dumps({
            "filters": {"lang": ["klingon"]}, "format": "zip",
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/api/download/filter", data=body,
            headers={"Content-Type": "application/json", "X-API-Key": "k"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()