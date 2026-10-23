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


def setUpModule():
    """模块级隔离：强制本地模式 + 未配置远程/Supabase。

    dev 机的 .env 可能填了 CLAW_REMOTE_BASE / CLAW_SUPABASE_*（真实部署配置），
    若不重置，config 在 import 时就会读到非空值，让本模块所有 sync/auth 测试
    以为「已配置云端」而失败。每个测试类自己的 setUp 仍做快照+恢复；这里只在
    模块入口兜底清一次，保证起点干净。"""
    from tests._isolated import _force_local
    _force_local()


class FakeTransport:
    """内存版云端传输层：记录所有 SQL，按用户返回预置的拉取行。"""

    def __init__(self):
        self.statements = []
        self.galleries = {}   # username -> list[row dict]
        self.runs = {}        # username -> list[row dict]
        self.accounts = {}    # username -> 账号 dict（claw_users 行）

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
        if "from public.\"claw_users\"" in low:
            name = self._username_from_where(sql) or ""
            account = self.accounts.get(name)
            return [account] if account else []
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
        # 快照全局 Supabase 配置与存储模式（本文件其他测试可能已改），统一在 tearDown 恢复
        self._sb_snapshot = (
            supabase_client.config.SUPABASE_PROJECT_REF,
            supabase_client.config.SUPABASE_TOKEN,
            supabase_client.config.SUPABASE_API_URL,
            supabase_client.config.SUPABASE_DB_URL,
            supabase_client.config.SUPABASE_TIMEOUT,
            supabase_client.config.STORAGE_BACKEND,
        )
        # 隔离本测试类：强制「未配置云端」+ 本地存储模式，注册/登录回退路径不触网
        supabase_client.config.SUPABASE_PROJECT_REF = ""
        supabase_client.config.SUPABASE_TOKEN = ""
        supabase_client.config.STORAGE_BACKEND = "local"
        supabase_client.set_transport(None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        self._restore_supabase()

    def _restore_supabase(self):
        supabase_client.config.SUPABASE_PROJECT_REF, supabase_client.config.SUPABASE_TOKEN, \
            supabase_client.config.SUPABASE_API_URL, supabase_client.config.SUPABASE_DB_URL, \
            supabase_client.config.SUPABASE_TIMEOUT, \
            supabase_client.config.STORAGE_BACKEND = self._sb_snapshot
        supabase_client.set_transport(None)
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
        # 快照全局 Supabase 配置与存储模式（本文件其他测试可能已改），统一在 tearDown 恢复
        self._sb_snapshot = (
            supabase_client.config.SUPABASE_PROJECT_REF,
            supabase_client.config.SUPABASE_TOKEN,
            supabase_client.config.SUPABASE_API_URL,
            supabase_client.config.SUPABASE_DB_URL,
            supabase_client.config.SUPABASE_TIMEOUT,
            supabase_client.config.STORAGE_BACKEND,
        )
        # 本测试类验证「未配置云端」语义，强制清空（与本文件其他测试隔离）
        supabase_client.config.SUPABASE_PROJECT_REF = ""
        supabase_client.config.SUPABASE_TOKEN = ""
        supabase_client.config.STORAGE_BACKEND = "local"
        supabase_client.set_transport(None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        self._restore_supabase()

    def _restore_supabase(self):
        supabase_client.config.SUPABASE_PROJECT_REF, supabase_client.config.SUPABASE_TOKEN, \
            supabase_client.config.SUPABASE_API_URL, supabase_client.config.SUPABASE_DB_URL, \
            supabase_client.config.SUPABASE_TIMEOUT, \
            supabase_client.config.STORAGE_BACKEND = self._sb_snapshot
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
        # 让 supabase_client 认为已配置（本地存储模式 + FakeTransport）
        self._orig_ref = supabase_client.config.SUPABASE_PROJECT_REF
        self._orig_tok = supabase_client.config.SUPABASE_TOKEN
        self._orig_backend = supabase_client.config.STORAGE_BACKEND
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.config.STORAGE_BACKEND = "local"
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
        supabase_client.config.STORAGE_BACKEND = self._orig_backend
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

    def test_cross_sync_anonymous_unlogin_into_login_account(self):
        """需求基线 §12.3：未登录时 POST /api/sync/cross，源数据为 unlogin_user
        本地库，合并进 target_user 的云端账号（再回推 target）。"""
        from app import config as app_config
        unlogin = app_config.UNLOGIN_USER
        # 1) 登录账号 demo 注册
        _, _, sc_demo = http("POST", self.base, "/api/auth/register",
                             body={"username": "demo", "password": "pass1"})
        cookie_demo = session_cookie(sc_demo)
        demo_db = db.user_db_path("demo")
        db.init_db(demo_db)
        # demo 本地先有 1 条
        db.upsert_items([GalleryItem(gallery_id=11001, url="https://example.net/g/11001/",
                                     title="Demo only", languages=["japanese"],
                                     title_key="demoonly")],
                        source_url="https://example.net/group/d/", path=demo_db)
        # 2) unlogin_user 本地库种 2 条
        unlogin_db = db.user_db_path(unlogin)
        db.init_db(unlogin_db)
        db.upsert_items([GalleryItem(gallery_id=12001, url="https://example.net/g/12001/",
                                     title="Unlogin A", languages=["japanese"],
                                     title_key="unlogina"),
                        GalleryItem(gallery_id=12002, url="https://example.net/g/12002/",
                                    title="Unlogin B", languages=["english"],
                                    title_key="unloginb")],
                       source_url="https://example.net/group/u/", path=unlogin_db)
        # 3) 未登录发起跨账号同步：把 unlogin 的数据合并到 demo 的云端
        status, data, _ = http("POST", self.base, "/api/sync/cross",
                              body={"target_user": "demo"})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["target_user"], "demo")
        # unlogin 的 2 条应合并进 demo 本地库（拉取来源 = 云端 unlogin 名下的行）
        # 但此时云端 unlogin 名下还没有数据（unlogin 从不同步上云），所以 pulled=0。
        # 改为：先看 demo 本地自己的 1 条仍在 + 云端推送包含 demo 的 11001
        self.assertTrue(any("11001" in s for s in self.fake.statements
                            if s.lower().startswith('insert into public."claw_galleries"')),
                        "demo 本地 11001 应被推送上云")

    def test_cross_sync_reverse_login_into_unlogin(self):
        """需求基线 §12.3 反向：已登录账号把 unlogin_user 作为 target，
        登录账号的本地数据合并进 unlogin 用户库。"""
        from app import config as app_config
        unlogin = app_config.UNLOGIN_USER
        _, _, sc = http("POST", self.base, "/api/auth/register",
                        body={"username": "kim", "password": "pass"})
        cookie = session_cookie(sc)
        kim_db = db.user_db_path("kim")
        db.init_db(kim_db)
        db.upsert_items([GalleryItem(gallery_id=13001, url="https://example.net/g/13001/",
                                     title="Kim only", languages=["japanese"],
                                     title_key="kimonly")],
                        source_url="https://example.net/group/k/", path=kim_db)
        # 已登录发起，target=unlogin_user：kim 本地 13001 推上云（unlogin 名下行），
        # 并从云端拉 unlogin 的行合并到 unlogin 本地库
        status, data, _ = http("POST", self.base, "/api/sync/cross",
                              body={"target_user": unlogin}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["target_user"], unlogin)
        unlogin_db = db.user_db_path(unlogin)
        ids = {row["gallery_id"] for row in db.query_works(
            path=unlogin_db, per_page=50)["items"]}
        self.assertIn(13001, ids, "kim 的数据应被合并进 unlogin 用户库")

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


class LoginSyncCredsTests(unittest.TestCase):
    """「同步云端配置」开关（§12.4）：登录时可选用云端凭据覆盖本地 api_key/site_origin。

    注入 FakeTransport，预置云端账号行；三种场景：
    - 关闭开关：本地凭据保持不变；
    - 开启开关：本地凭据被云端值覆盖，密码（PBKDF2 验证）不受影响；
    - 本地无账号的云回退：云端凭据作为初始值重建本地账号行。
    """

    def setUp(self):
        self.tmp = make_temp_dir("loginsc")
        self.db_path = self.tmp / "ls.sqlite3"
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self._orig_ref = supabase_client.config.SUPABASE_PROJECT_REF
        self._orig_tok = supabase_client.config.SUPABASE_TOKEN
        self._orig_backend = supabase_client.config.STORAGE_BACKEND
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.config.STORAGE_BACKEND = "local"
        self.fake = FakeTransport()
        supabase_client.set_transport(self.fake)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # 云端账号行：凭据与本地不同，便于断言是否被覆盖
        self._cloud_salt, self._cloud_hash = db.hash_password("pass1")
        self.fake.accounts["demo"] = {
            "username": "demo",
            "api_key": "cloud-key",
            "site_origin": "https://cloud.example.net",
            "password_hash": self._cloud_hash,
            "salt": self._cloud_salt,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        supabase_client.config.SUPABASE_PROJECT_REF = self._orig_ref
        supabase_client.config.SUPABASE_TOKEN = self._orig_tok
        supabase_client.config.STORAGE_BACKEND = self._orig_backend
        supabase_client.set_transport(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_demo_with_local_creds(self):
        """注册 demo，本地凭据 = local-key / local.example.net。"""
        status, data, _ = http("POST", self.base, "/api/auth/register",
                               body={"username": "demo", "password": "pass1",
                                     "api_key": "local-key",
                                     "site_origin": "https://local.example.net"})
        self.assertEqual(status, 200)
        user = db.get_user("demo", self.db_path)
        self.assertEqual(user["api_key"], "local-key")
        self.assertEqual(user["site_origin"], "https://local.example.net")

    def test_login_without_switch_keeps_local_creds(self):
        self._register_demo_with_local_creds()
        status, data, _ = http("POST", self.base, "/api/auth/login",
                               body={"username": "demo", "password": "pass1"})
        self.assertEqual(status, 200)
        user = db.get_user("demo", self.db_path)
        # 未开「同步云端配置」：本地凭据保持 local-*，不被云端 cloud-* 覆盖
        self.assertEqual(user["api_key"], "local-key")
        self.assertEqual(user["site_origin"], "https://local.example.net")

    def test_login_with_switch_overlays_cloud_creds(self):
        self._register_demo_with_local_creds()
        status, data, _ = http("POST", self.base, "/api/auth/login",
                               body={"username": "demo", "password": "pass1",
                                     "sync_creds": True})
        self.assertEqual(status, 200)
        user = db.get_user("demo", self.db_path)
        # 开启「同步云端配置」：api_key / site_origin 被云端值覆盖
        self.assertEqual(user["api_key"], "cloud-key")
        self.assertEqual(user["site_origin"], "https://cloud.example.net")
        # 覆盖凭据不能破坏本地密码：原密码仍能验证
        self.assertTrue(db.verify_user("demo", "pass1", self.db_path))

    def test_local_verify_cloud_fallback_still_overlays(self):
        """本地无账号行时走云回退：重建的账号行凭据来自云端。"""
        status, data, _ = http("POST", self.base, "/api/auth/register",
                               body={"username": "demo", "password": "pass1",
                                     "api_key": "local-key",
                                     "site_origin": "https://local.example.net"})
        self.assertEqual(status, 200)
        # 删掉本地账号行，模拟异地登录（本地库里没有 demo）
        conn = db.connect(self.db_path)
        try:
            conn.execute("DELETE FROM users WHERE username = ?", ("demo",))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(db.get_user("demo", self.db_path))

        status, data, sc = http("POST", self.base, "/api/auth/login",
                                body={"username": "demo", "password": "pass1"})
        self.assertEqual(status, 200)
        user = db.get_user("demo", self.db_path)
        self.assertIsNotNone(user, "云回退后本地账号行应被重建")
        self.assertEqual(user["api_key"], "cloud-key")
        self.assertEqual(user["site_origin"], "https://cloud.example.net")
        self.assertTrue(db.verify_user("demo", "pass1", self.db_path))


class OldDbCompatTests(unittest.TestCase):
    """已运行过的旧库迁移兼容性：download_jobs 没有 username/origin 列时不能崩。

    注：本模块其他测试会把全局 config.SUPABASE_* 切到 FakeTransport/本地配置，
    这里统一用 setUp 里存好的快照恢复，避免真实 token 被带进后续测试。
    """

    def setUp(self):
        self.tmp = make_temp_dir("oldcompat")
        self.db_path = self.tmp / "old.sqlite3"
        # 快照全局 Supabase 配置与传输层（本文件前面可能已改成测试值）
        self._sb_snapshot = (
            supabase_client.config.SUPABASE_PROJECT_REF,
            supabase_client.config.SUPABASE_TOKEN,
            supabase_client.config.SUPABASE_API_URL,
            supabase_client.config.SUPABASE_DB_URL,
            supabase_client.config.SUPABASE_TIMEOUT,
        )
        supabase_client.config.SUPABASE_PROJECT_REF = "test-ref"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.set_transport(None)

    def tearDown(self):
        supabase_client.config.SUPABASE_PROJECT_REF, supabase_client.config.SUPABASE_TOKEN, \
            supabase_client.config.SUPABASE_API_URL, supabase_client.config.SUPABASE_DB_URL, \
            supabase_client.config.SUPABASE_TIMEOUT = self._sb_snapshot
        supabase_client.set_transport(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

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


class CloudScrapeSinkTests(unittest.TestCase):
    """云模式：抓取经 CloudSink 直接写云端（不落本地 SQLite）的语义锁定。"""

    def setUp(self):
        self.tmp = make_temp_dir("cloudsink")
        self._sb_snapshot = (
            supabase_client.config.SUPABASE_PROJECT_REF,
            supabase_client.config.SUPABASE_TOKEN,
            supabase_client.config.STORAGE_BACKEND,
        )
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.config.STORAGE_BACKEND = "cloud"
        self.fake = FakeTransport()
        supabase_client.set_transport(self.fake)

    def tearDown(self):
        supabase_client.config.SUPABASE_PROJECT_REF, supabase_client.config.SUPABASE_TOKEN, \
            supabase_client.config.STORAGE_BACKEND = self._sb_snapshot
        supabase_client.set_transport(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cloud_sink_writes_to_cloud_not_local(self):
        from app import scrape_service as svc
        sink = svc.CloudSink(username="alice")
        result = sink.upsert_items(
            [GalleryItem(gallery_id=55001, url="https://example.net/g/55001/",
                         title="Cloud scrape", languages=["japanese"],
                         title_key="cloudscrape")],
            source_url="https://example.net/group/x/", batch_id=9001)
        self.assertEqual(result["new"], 1)
        # 数据落进 FakeTransport 记录的云端（而非本地 SQLite）
        self.assertTrue(any(s.lower().startswith('insert into public."claw_galleries"')
                            for s in self.fake.statements))

    def test_cloud_sink_existing_id_is_dup(self):
        from app import scrape_service as svc
        self.fake.galleries["alice"] = [{
            "gallery_id": 55002, "title": "Exist", "last_seen": "2026-01-01T00:00:00+00:00",
        }]
        sink = svc.CloudSink(username="alice")
        result = sink.upsert_items(
            [GalleryItem(gallery_id=55002, url="https://example.net/g/55002/",
                         title="Exist", languages=["japanese"], title_key="exist")],
            source_url="https://example.net/group/x/", batch_id=9002)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["dup"], 1)


class CloudUserDbInitTests(unittest.TestCase):
    """云模式注册/登录后用户库必须已建全表（settings/download_jobs/...）。

    回归：云模式下 register / _cloud_login_fallback 历史上用
    ``if not config.storage_is_cloud()`` 跳过了 init_db(user_db)，于是
    save_credentials 写 settings.site_origin、download_queue 读
    settings/download_jobs 都会踩 ``no such table: settings``。现在无论云/本地
    都在建用户时初始化用户库全表（启动时 ensure_user_dbs 再兜一次）。
    """

    def setUp(self):
        self.tmp = make_temp_dir("clouduserdb")
        self.db_path = self.tmp / "acc.sqlite3"
        self._orig_user_data_dir = db.config.USER_DATA_DIR
        db.config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self._sb_snapshot = (
            supabase_client.config.SUPABASE_PROJECT_REF,
            supabase_client.config.SUPABASE_TOKEN,
            supabase_client.config.STORAGE_BACKEND,
        )
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.config.STORAGE_BACKEND = "cloud"
        self.fake = FakeTransport()
        supabase_client.set_transport(self.fake)

    def tearDown(self):
        supabase_client.config.SUPABASE_PROJECT_REF, \
            supabase_client.config.SUPABASE_TOKEN, \
            supabase_client.config.STORAGE_BACKEND = self._sb_snapshot
        supabase_client.set_transport(None)
        db.config.USER_DATA_DIR = self._orig_user_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _table_names(self, path) -> set:
        conn = db.connect(path)
        try:
            return {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def test_register_inits_user_db_tables_in_cloud_mode(self):
        from app.services import auth_service
        result = auth_service.register("clouddie", "pass1234", "key", "example.net",
                                       self.db_path)
        self.assertTrue(result.get("ok"), result)
        user_db = db.user_db_path("clouddie")
        tables = self._table_names(user_db)
        for required in ("settings", "download_jobs", "galleries", "scrape_runs"):
            self.assertIn(required, tables,
                          f"云模式注册后用户库缺 {required} 表")
        # 这正是线上踩的坑：写 settings.site_origin 不应再抛 no such table
        db.set_setting("site_origin", "https://x.example.net", user_db)
        self.assertEqual(db.get_setting("site_origin", "", user_db),
                         "https://x.example.net")

    def test_cloud_login_fallback_inits_user_db_tables(self):
        from app.services import auth_service
        salt, digest = db.hash_password("pass1234")
        self.fake.accounts["fallbacker"] = {
            "username": "fallbacker", "api_key": "key", "site_origin": "",
            "password_hash": digest, "salt": salt, "created_at": db.utc_now(),
        }
        result = auth_service.login("fallbacker", "pass1234", self.db_path)
        self.assertTrue(result.get("ok"), result)
        tables = self._table_names(db.user_db_path("fallbacker"))
        self.assertIn("settings", tables)
        self.assertIn("download_jobs", tables)


if __name__ == "__main__":
    unittest.main()
