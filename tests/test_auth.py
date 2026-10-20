"""多用户认证、用户数据隔离、Supabase 同步测试（全部离线）。

覆盖：
- 注册 / 登录 / 会话 / 退出（HTTP 烟测）
- 两个登录用户的爬取数据互不串库
- 未配置云端时 /api/sync/status 不报 500、/api/sync 返回可读错误
- 注入 FakeTransport 后建表、拉取合并、推送、用户隔离
"""
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import db, supabase_client
from app.parser import GalleryItem
from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def http(method, base, path, *, body=None, cookie=None):
    """轻量 HTTP 客户端，支持 cookie；对 4xx/5xx 也返回 (status, payload, set_cookie)。"""
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        set_cookie = exc.headers.get("Set-Cookie", "")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        return exc.code, payload, set_cookie
    with response:
        status = response.status
        raw = response.read()
        set_cookie = response.headers.get("Set-Cookie", "")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload, set_cookie


def session_cookie(set_cookie_header: str) -> str:
    """从 Set-Cookie 头里取 claw_session=... 的值。"""
    for part in set_cookie_header.split(";"):
        part = part.strip()
        if part.startswith("claw_session="):
            return part
    return ""


class FakeTransport:
    """内存版云端传输层：记录所有 SQL，按用户返回预置的拉取行。"""

    def __init__(self):
        self.statements = []
        self.galleries = {}   # username -> list[row dict]
        self.runs = {}        # username -> list[row dict]

    @staticmethod
    def _username_from_where(sql):
        low = sql.lower()
        if "where" not in low:
            return None
        tail = sql[low.index("where"):]
        # where "username" = 'xxx'
        if "'" not in tail:
            return None
        first = tail.index("'") + 1
        second = tail.index("'", first)
        return tail[first:second]

    def query(self, sql, *, read_only=False):
        self.statements.append(sql)
        low = sql.lower().strip()
        if low.startswith("select table_name from information_schema"):
            return [
                {"table_name": "claw_users"},
                {"table_name": "claw_galleries"},
                {"table_name": "claw_scrape_runs"},
            ]
        if "from public.\"claw_galleries\"" in low and "where" in low:
            name = self._username_from_where(sql) or ""
            return list(self.galleries.get(name, []))
        if "from public.\"claw_scrape_runs\"" in low and "where" in low:
            name = self._username_from_where(sql) or ""
            return list(self.runs.get(name, []))
        # DDL / insert / alter / revoke / create index：一律视为成功，不返回行
        return []


class AuthAndIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("auth")
        self.db_path = self.tmp / "auth.sqlite3"
        # 把用户库目录指到临时区，避免污染主 data/users
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_register_login_me_logout(self):
        # 注册
        status, data, sc = http("POST", self.base, "/api/auth/register",
                               body={"username": "alice", "password": "secret",
                                     "site_origin": "example.net"})
        self.assertEqual(status, 200)
        self.assertEqual(data["username"], "alice")
        cookie = session_cookie(sc)
        self.assertIn("claw_session=", cookie)

        # me 已登录
        status, data, _ = http("GET", self.base, "/api/auth/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["logged_in"])
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["user"]["site_origin"], "https://example.net")
        # Key 不回显：只有 has_api_key 布尔
        self.assertNotIn("api_key", data["user"])

        # 退出
        status, _, sc = http("POST", self.base, "/api/auth/logout", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", sc)

        # me 未登录
        status, data, _ = http("GET", self.base, "/api/auth/me")
        self.assertFalse(data["logged_in"])

    def test_register_duplicate_rejected(self):
        http("POST", self.base, "/api/auth/register",
           body={"username": "bob", "password": "pass"})
        status, data, _ = http("POST", self.base, "/api/auth/register",
                             body={"username": "bob", "password": "other"})
        self.assertEqual(status, 409)

    def test_login_wrong_password(self):
        http("POST", self.base, "/api/auth/register",
           body={"username": "carol", "password": "right"})
        status, _, _ = http("POST", self.base, "/api/auth/login",
                           body={"username": "carol", "password": "wrong"})
        self.assertEqual(status, 401)

    def test_two_users_data_isolated(self):
        # alice 和 dave 各自注册
        _, _, sc_a = http("POST", self.base, "/api/auth/register",
                         body={"username": "alice2", "password": "pass"})
        cookie_a = session_cookie(sc_a)
        _, _, sc_d = http("POST", self.base, "/api/auth/register",
                         body={"username": "dave2", "password": "pass"})
        cookie_d = session_cookie(sc_d)

        # alice 的库里写入一条作品
        alice_db = db.user_db_path("alice2")
        db.init_db(alice_db)
        db.upsert_items([GalleryItem(gallery_id=5001,
                                     url="https://example.net/g/5001/",
                                     title="Alice's work", languages=["japanese"],
                                     title_key="alicework")],
                       source_url="https://example.net/group/a/", path=alice_db)
        # dave 的库里写入另一条
        dave_db = db.user_db_path("dave2")
        db.init_db(dave_db)
        db.upsert_items([GalleryItem(gallery_id=6001,
                                     url="https://example.net/g/6001/",
                                     title="Dave's work", languages=["english"],
                                     title_key="davework")],
                       source_url="https://example.net/group/b/", path=dave_db)

        # alice 看到自己的 5001，看不到 dave 的 6001
        status, data, _ = http("GET", self.base, "/api/works?per_page=50", cookie=cookie_a)
        self.assertEqual(status, 200)
        ids = {item["gallery_id"] for item in data["items"]}
        self.assertIn(5001, ids)
        self.assertNotIn(6001, ids)

        # dave 看到自己的 6001，看不到 alice 的 5001
        status, data, _ = http("GET", self.base, "/api/works?per_page=50", cookie=cookie_d)
        ids = {item["gallery_id"] for item in data["items"]}
        self.assertIn(6001, ids)
        self.assertNotIn(5001, ids)

    def test_anonymous_still_works_without_login(self):
        """匿名兼容路径：未登录也能查 /api/meta 和 /api/works。"""
        status, data, _ = http("GET", self.base, "/api/meta")
        self.assertEqual(status, 200)
        self.assertFalse(data["user"]["logged_in"])
        status, _, _ = http("GET", self.base, "/api/works?per_page=10")
        self.assertEqual(status, 200)

    def test_credentials_not_echo_key(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                       body={"username": "erin", "password": "pass", "api_key": "supersecretkey"})
        cookie = session_cookie(sc)
        # 读设置：只有 has_api_key，不回显原文
        status, data, _ = http("GET", self.base, "/api/settings/credentials", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["has_api_key"])
        self.assertNotIn("api_key", data)
        # 整个响应序列化里不应出现明文
        self.assertNotIn("supersecretkey", json.dumps(data))

    def test_login_required_for_sync(self):
        status, _, _ = http("POST", self.base, "/api/sync", body={})
        self.assertEqual(status, 401)


class SyncStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("syncstatus")
        self.db_path = self.tmp / "sync.sqlite3"
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # 确保云端未配置
        self._orig_ref = supabase_client.config.SUPABASE_PROJECT_REF
        self._orig_tok = supabase_client.config.SUPABASE_TOKEN
        supabase_client.config.SUPABASE_PROJECT_REF = ""
        supabase_client.config.SUPABASE_TOKEN = ""
        supabase_client.set_transport(None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        supabase_client.config.SUPABASE_PROJECT_REF = self._orig_ref
        supabase_client.config.SUPABASE_TOKEN = self._orig_tok
        supabase_client.set_transport(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_status_not_configured(self):
        status, data, _ = http("GET", self.base, "/api/sync/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["configured"])

    def test_sync_when_not_configured_returns_readable_error(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                        body={"username": "frank", "password": "pass"})
        cookie = session_cookie(sc)
        status, data, _ = http("POST", self.base, "/api/sync", body={}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("云端", data["error"])


class SyncFlowTests(unittest.TestCase):
    """注入 FakeTransport，测试拉取合并 → 推送回云端的完整流程。"""

    def setUp(self):
        self.tmp = make_temp_dir("syncflow")
        self.db_path = self.tmp / "sf.sqlite3"
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        # 让 supabase_client 认为已配置
        self._orig_ref = supabase_client.config.SUPABASE_PROJECT_REF
        self._orig_tok = supabase_client.config.SUPABASE_TOKEN
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        self.fake = FakeTransport()
        supabase_client.set_transport(self.fake)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        supabase_client.config.SUPABASE_PROJECT_REF = self._orig_ref
        supabase_client.config.SUPABASE_TOKEN = self._orig_tok
        supabase_client.set_transport(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pull_merges_cloud_into_local(self):
        # 注册用户 gina
        _, _, sc = http("POST", self.base, "/api/auth/register",
                       body={"username": "gina", "password": "pass"})
        cookie = session_cookie(sc)
        gina_db = db.user_db_path("gina")
        db.init_db(gina_db)

        # 云端有一条 gina 的作品（拉取阶段会返回它）
        self.fake.galleries["gina"] = [{
            "gallery_id": 7001, "media_id": 7, "title": "Cloud work",
            "title_pretty": "", "title_original": "", "title_key": "cloud",
            "original_key": "", "url": "https://example.net/g/7001/", "thumbnail": "",
            "languages": "[]", "primary_language": "japanese", "tags": "[]",
            "artists": "[]", "groups": "[]", "parodies": "[]", "characters": "[]",
            "categories": "[]", "cover_file": "", "cover_fetched_at": "",
            "scanlator": "", "uncensored": 0, "want_download": 0, "downloaded": 0,
            "downloaded_at": "", "hidden": 0, "site_favorite": 0, "from_scrape": 1,
            "batch_id": 0, "num_pages": None, "num_favorites": None,
            "upload_date": "", "source_url": "", "detail_fetched_at": "",
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-02T00:00:00+00:00",
        }]

        status, data, _ = http("POST", self.base, "/api/sync", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["pulled"]["galleries"], 1)
        # 合并后本地能看到 7001
        status, works, _ = http("GET", self.base, "/api/works?per_page=50", cookie=cookie)
        ids = {item["gallery_id"] for item in works["items"]}
        self.assertIn(7001, ids)
        # 推送阶段向云端发了 INSERT
        self.assertTrue(any(s.lower().startswith('insert into public."claw_galleries"')
                            for s in self.fake.statements))

    def test_push_sends_local_runs(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                       body={"username": "heidi", "password": "pass"})
        cookie = session_cookie(sc)
        heidi_db = db.user_db_path("heidi")
        db.init_db(heidi_db)
        # 本地写入一条作品 + 一个批次
        db.upsert_items([GalleryItem(gallery_id=8001,
                                     url="https://example.net/g/8001/",
                                     title="Local work", languages=["japanese"],
                                     title_key="local")],
                       source_url="https://example.net/group/h/", path=heidi_db)

        status, data, _ = http("POST", self.base, "/api/sync", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["pushed"]["galleries"], 1)
        # 确认推送的 INSERT 语句里包含 8001
        inserts = [s for s in self.fake.statements
                   if s.lower().startswith('insert into public."claw_galleries"')]
        self.assertTrue(any("8001" in s for s in inserts))

    def test_user_isolation_in_cloud_keys(self):
        # 两个用户的云端行按 username 隔离：gina 拉到的不应是 heidi 推的
        _, _, sc_g = http("POST", self.base, "/api/auth/register",
                         body={"username": "gina2", "password": "pass"})
        cookie_g = session_cookie(sc_g)
        _, _, sc_h = http("POST", self.base, "/api/auth/register",
                         body={"username": "heidi2", "password": "pass"})
        cookie_h = session_cookie(sc_h)

        # heidi2 本地有 9001，gina2 没有
        h_db = db.user_db_path("heidi2")
        db.init_db(h_db)
        db.upsert_items([GalleryItem(gallery_id=9001,
                                     url="https://example.net/g/9001/",
                                     title="H only", languages=["english"],
                                     title_key="honly")],
                       source_url="https://example.net/group/h2/", path=h_db)

        # heidi2 同步推到云端
        http("POST", self.base, "/api/sync", body={}, cookie=cookie_h)
        # gina2 同步：拉取阶段云端没有 gina2 的行，所以拉到 0；本地也空
        status, data, _ = http("POST", self.base, "/api/sync", body={}, cookie=cookie_g)
        self.assertEqual(data["pulled"]["galleries"], 0)
        # gina2 不应看到 heidi2 的 9001
        _, works, _ = http("GET", self.base, "/api/works?per_page=50", cookie=cookie_g)
        ids = {item["gallery_id"] for item in works["items"]}
        self.assertNotIn(9001, ids)


class OldDbCompatTests(unittest.TestCase):
    """已运行过的旧库迁移兼容性：download_jobs 没有 username/origin 列时不能崩。"""

    def setUp(self):
        self.tmp = make_temp_dir("oldcompat")
        self.db_path = self.tmp / "old.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_old_db(self):
        """模拟运行过一天的老库：galleries 全列齐全，download_jobs 是老结构（无 username/origin）。"""
        conn = db.connect(self.db_path)
        try:
            gcols = (
                "gallery_id INTEGER PRIMARY KEY, media_id INTEGER, title TEXT, title_pretty TEXT, "
                "title_original TEXT, title_key TEXT, original_key TEXT, url TEXT, thumbnail TEXT, "
                "languages TEXT, primary_language TEXT, tags TEXT, artists TEXT, groups TEXT, "
                "parodies TEXT, characters TEXT, categories TEXT, cover_file TEXT, "
                "cover_fetched_at TEXT, scanlator TEXT, uncensored INTEGER, want_download INTEGER, "
                "downloaded INTEGER, downloaded_at TEXT, hidden INTEGER, site_favorite INTEGER, "
                "from_scrape INTEGER, batch_id INTEGER, num_pages INTEGER, num_favorites INTEGER, "
                "upload_date TEXT, source_url TEXT, detail_fetched_at TEXT, first_seen TEXT, "
                "last_seen TEXT"
            )
            conn.executescript(
                f"CREATE TABLE galleries ({gcols});\n"
                "CREATE TABLE download_jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "gallery_id INTEGER NOT NULL, title TEXT, format TEXT, source TEXT, batch_id INTEGER, "
                "status TEXT, total_bytes INTEGER, done_bytes INTEGER, file_path TEXT, error TEXT, "
                "created_at TEXT, started_at TEXT, finished_at TEXT);\n"
                "INSERT INTO download_jobs (gallery_id, title, format, source, status, created_at) "
                "VALUES (1, 'old job', 'zip', 'manual', 'queued', '2026-10-19');\n"
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);\n"
                "CREATE TABLE scrape_runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, "
                "status TEXT, started_at TEXT);\n"
                "CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT, salt TEXT, "
                "api_key TEXT, site_origin TEXT, created_at TEXT);\n"
                "CREATE TABLE sessions (token TEXT PRIMARY KEY, username TEXT, created_at TEXT, "
                "expires_at TEXT);"
            )
            conn.commit()
        finally:
            conn.close()

    def test_init_db_migrates_old_download_jobs(self):
        self._build_old_db()
        # 老库跑 init_db：必须补 username/origin 列、建索引、不报错
        db.init_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(download_jobs)")]
            self.assertIn("username", cols)
            self.assertIn("origin", cols)
            idx = [row[1] for row in conn.execute("PRAGMA index_list(download_jobs)")]
            self.assertIn("idx_jobs_username", idx)
            # 旧任务行保留，新列默认空
            row = conn.execute(
                "SELECT username, origin, title FROM download_jobs WHERE gallery_id = 1"
            ).fetchone()
            self.assertEqual(row["username"], "")
            self.assertEqual(row["origin"], "")
            self.assertEqual(row["title"], "old job")
        finally:
            conn.close()

    def test_init_db_idempotent_on_new_db(self):
        # 新库连续 init 两次都不应报错（幂等）
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(download_jobs)")]
            self.assertIn("username", cols)
            self.assertIn("origin", cols)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
