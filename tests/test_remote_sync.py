"""远程 VPS 代理的 auth + sync 测试（全部离线，用 fake 远程客户端）。

验证 C 端在设了 CLAW_REMOTE_BASE 后：
- 注册/登录改走 VPS 的 /api/remote/account，且**注册远程失败不写本地**
- 拉取/推送改走 VPS 的 /api/remote/sync（本机无需 Supabase 凭据）
- CLAW_REMOTE_BASE 留空时，维持现状（直连 Supabase，向后兼容）
- 新增的 /api/remote/* 端点本身（云模式 VPS 侧）复用 supabase_client 且密钥校验生效
"""
import json
import shutil
import threading
import unittest
import uuid
from pathlib import Path

from app import db, config, remote_client, supabase_client
from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def http(method, base, path, *, body=None, cookie=None, headers=None):
    import urllib.error
    import urllib.request
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    if cookie:
        hdrs["Cookie"] = cookie
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            raw = response.read()
            set_cookie = response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
        set_cookie = exc.headers.get("Set-Cookie", "")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload, set_cookie


def session_cookie(set_cookie_header: str) -> str:
    for part in set_cookie_header.split(";"):
        part = part.strip()
        if part.startswith("claw_session="):
            return part
    return ""


class FakeRemoteClient:
    """内存版远程 VPS：记录调用，返回预置账号/拉取/推送结果，不触网。"""

    def __init__(self):
        self.accounts = {}   # username -> 账号 dict
        self.pull_rows = {}  # username -> {"galleries": [...], "runs": [...]}
        self.upsert_calls = []
        self.pull_calls = []
        self.push_calls = []
        self.fail = False    # 置 True 时所有方法抛 RemoteError

    def _maybe_fail(self):
        if self.fail:
            raise remote_client.RemoteError("模拟远程故障", 503)

    def fetch_account(self, username):
        self._maybe_fail()
        return self.accounts.get(username)

    def upsert_account(self, username, *, api_key, site_origin,
                      password_hash, salt, created_at):
        self._maybe_fail()
        self.upsert_calls.append(
            {"username": username, "api_key": api_key, "site_origin": site_origin,
             "password_hash": password_hash, "salt": salt, "created_at": created_at})
        self.accounts[username] = {
            "username": username, "password_hash": password_hash, "salt": salt,
            "api_key": api_key, "site_origin": site_origin, "created_at": created_at,
        }
        return {"ok": True, "username": username}

    def pull(self, username):
        self._maybe_fail()
        self.pull_calls.append(username)
        rows = self.pull_rows.get(username, {"galleries": [], "runs": []})
        return {"ok": True, "username": username,
                "galleries": rows["galleries"], "runs": rows["runs"],
                "counts": {"galleries": len(rows["galleries"]),
                            "runs": len(rows["runs"])}}

    def push(self, username, galleries, runs):
        self._maybe_fail()
        self.push_calls.append((username, galleries, runs))
        return {"ok": True, "username": username,
                "pushed": {"galleries": len(galleries), "runs": len(runs)}}


class _RemoteBase:
    """公共 setUp/tearDown：本地模式 + 无 Supabase 凭据 + 设 CLAW_REMOTE_BASE + 注入 fake。"""

    def _remote_setup(self, tmp_name, with_supabase=False):
        self.tmp = make_temp_dir(tmp_name)
        self.db_path = self.tmp / "main.sqlite3"
        self._orig_user_data_dir = config.USER_DATA_DIR
        config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        # 快照远程 + Supabase 配置
        self._remote_snapshot = (config.REMOTE_BASE, config.REMOTE_AUTH_TOKEN)
        self._sb_snapshot = (supabase_client.config.SUPABASE_PROJECT_REF,
                             supabase_client.config.SUPABASE_TOKEN,
                             supabase_client.config.STORAGE_BACKEND)
        # 默认场景：无 Supabase 凭据，只有远程 VPS（这正是本次改造要验证的核心）
        supabase_client.config.SUPABASE_PROJECT_REF = ""
        supabase_client.config.SUPABASE_TOKEN = ""
        supabase_client.config.STORAGE_BACKEND = "local"
        supabase_client.set_transport(None)
        config.REMOTE_AUTH_TOKEN = "test-token"
        if with_supabase:
            supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
            supabase_client.config.SUPABASE_TOKEN = "test-token"
        config.REMOTE_BASE = "http://127.0.0.1:0"
        self.fake = FakeRemoteClient()
        remote_client.set_client(self.fake)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _remote_teardown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        config.USER_DATA_DIR = self._orig_user_data_dir
        config.REMOTE_BASE, config.REMOTE_AUTH_TOKEN = self._remote_snapshot
        (supabase_client.config.SUPABASE_PROJECT_REF,
         supabase_client.config.SUPABASE_TOKEN,
         supabase_client.config.STORAGE_BACKEND) = self._sb_snapshot
        supabase_client.set_transport(None)
        remote_client.reset_client()
        shutil.rmtree(self.tmp, ignore_errors=True)


class RemoteAuthTests(_RemoteBase, unittest.TestCase):
    def setUp(self):
        self._remote_setup("remote_auth")

    def tearDown(self):
        self._remote_teardown()

    def test_register_via_remote_writes_local_only_on_success(self):
        status, data, sc = http("POST", self.base, "/api/auth/register",
                                body={"username": "zoe", "password": "secret",
                                      "api_key": "k1", "site_origin": "example.net"})
        self.assertEqual(status, 200)
        self.assertTrue(data["cloud_registered"], "注册应标记已写远程")
        self.assertIn("claw_session=", sc)
        # 远程被调了一次 upsert（带凭据）
        self.assertEqual(len(self.fake.upsert_calls), 1)
        call = self.fake.upsert_calls[0]
        self.assertEqual(call["username"], "zoe")
        self.assertEqual(call["api_key"], "k1")
        # 本地 users 表也建了行（远程成功后才落本地）
        self.assertIsNotNone(db.get_user("zoe", self.db_path))

    def test_register_remote_failure_writes_nothing_local(self):
        self.fake.fail = True
        status, data, _ = http("POST", self.base, "/api/auth/register",
                                body={"username": "zoe", "password": "secret"})
        self.assertIn(status, (400, 502))
        # 远程失败 → 本地绝不能有该账号
        self.assertIsNone(db.get_user("zoe", self.db_path))

    def test_login_via_remote_rebuilds_user_without_supabase(self):
        # 预置远程账号（本地没有的用户），无 Supabase 凭据也能登录
        import hashlib
        from app import db as dbmod
        salt, digest = dbmod.hash_password("secret", salt="00" * 16)
        self.fake.accounts["zoe"] = {
            "username": "zoe", "password_hash": digest, "salt": salt,
            "api_key": "k9", "site_origin": "https://example.net", "created_at": "",
        }
        status, data, sc = http("POST", self.base, "/api/auth/login",
                                body={"username": "zoe", "password": "secret"})
        self.assertEqual(status, 200)
        self.assertIn("claw_session=", sc)
        # 本地 users 行被重建（沿用远程的哈希/盐/Key）
        row = db.get_user("zoe", self.db_path)
        self.assertIsNotNone(row)
        self.assertEqual(row["api_key"], "k9")

    def test_login_wrong_password_401(self):
        salt, digest = db.hash_password("secret", salt="00" * 16)
        self.fake.accounts["zoe"] = {
            "username": "zoe", "password_hash": digest, "salt": salt,
            "api_key": "", "site_origin": "", "created_at": "",
        }
        status, _, _ = http("POST", self.base, "/api/auth/login",
                            body={"username": "zoe", "password": "wrong"})
        self.assertEqual(status, 401)


class RemoteSyncTests(_RemoteBase, unittest.TestCase):
    def setUp(self):
        self._remote_setup("remote_sync")

    def tearDown(self):
        self._remote_teardown()

    def test_pull_via_remote_merges_local(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                         body={"username": "pam", "password": "pass"})
        cookie = session_cookie(sc)
        pam_db = db.user_db_path("pam")
        db.init_db(pam_db)
        # 远端 pull 返回一行
        self.fake.pull_rows["pam"] = {"galleries": [{
            "gallery_id": 8001, "media_id": 1, "title": "Remote work", "title_pretty": "",
            "title_original": "", "title_key": "remote", "original_key": "",
            "url": "https://example.net/g/8001/", "thumbnail": "", "languages": "[]",
            "primary_language": "japanese", "tags": "[]", "artists": "[]", "groups": "[]",
            "parodies": "[]", "characters": "[]", "categories": "[]", "cover_file": "",
            "cover_fetched_at": "", "scanlator": "", "uncensored": 0, "want_download": 0,
            "downloaded": 0, "downloaded_at": "", "hidden": 0, "site_favorite": 0,
            "from_scrape": 1, "batch_id": 0, "num_pages": None, "num_favorites": None,
            "upload_date": "", "source_url": "", "detail_fetched_at": "",
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-02T00:00:00+00:00",
        }], "runs": []}
        status, data, _ = http("POST", self.base, "/api/sync/pull", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("via_remote"))
        self.assertEqual(data["pulled"]["galleries"], 1)
        # 本地库现在能看到 8001
        status, works, _ = http("GET", self.base, "/api/works?per_page=50", cookie=cookie)
        ids = {item["gallery_id"] for item in works["items"]}
        self.assertIn(8001, ids)

    def test_push_via_remote_sends_local_rows(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                         body={"username": "kim", "password": "pass"})
        cookie = session_cookie(sc)
        kim_db = db.user_db_path("kim")
        db.init_db(kim_db)
        db.upsert_items([_make_item(9001)], source_url="https://example.net/group/x/",
                        path=kim_db)
        status, data, _ = http("POST", self.base, "/api/sync/push", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("via_remote"))
        # fake 收到了推送，且包含 9001
        self.assertEqual(len(self.fake.push_calls), 1)
        uname, g, r = self.fake.push_calls[0]
        self.assertEqual(uname, "kim")
        self.assertIn(9001, [row.get("gallery_id") for row in g])

    def test_sync_status_reports_remote(self):
        status, data, _ = http("GET", self.base, "/api/sync/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["configured"])
        self.assertTrue(data.get("via_remote"))


def _make_item(gid, title="Work", language="japanese"):
    from app.parser import GalleryItem
    return GalleryItem(gallery_id=gid, url=f"https://example.net/g/{gid}/",
                       title=title, languages=[language])


# ============================================================
# 向后兼容：CLAW_REMOTE_BASE 留空时走直连 Supabase（复用 test_auth 的 FakeTransport）
# ============================================================
class RemoteFallbackTests(_RemoteBase, unittest.TestCase):
    def setUp(self):
        self._remote_setup("remote_fallback", with_supabase=True)
        # 关键：远程**未**配置，只配了 Supabase → 应走直连
        config.REMOTE_BASE = ""
        from tests.test_auth import FakeTransport
        self.supabase_fake = FakeTransport()
        supabase_client.set_transport(self.supabase_fake)

    def tearDown(self):
        self._remote_teardown()

    def test_pull_goes_direct_supabase_when_remote_empty(self):
        _, _, sc = http("POST", self.base, "/api/auth/register",
                         body={"username": "danny", "password": "pass"})
        cookie = session_cookie(sc)
        danny_db = db.user_db_path("danny")
        db.init_db(danny_db)
        self.supabase_fake.galleries["danny"] = []
        status, data, _ = http("POST", self.base, "/api/sync/pull", body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(data.get("via_remote"))
        # 走了 Supabase 的 FakeTransport（记录了 SQL），而非 fake 远程客户端
        self.assertTrue(self.fake.pull_calls == [] and self.fake.push_calls == [])
        self.assertTrue(any(s.lower().startswith("select") for s in self.supabase_fake.statements))


# ============================================================
# 远程端点自身（云模式 VPS 侧）：复用 supabase_client + 密钥校验
# ============================================================
class RemoteEndpointsTests(unittest.TestCase):
    """起一个配了 Supabase FakeTransport 的服务（模拟 VPS），验证 /api/remote/*。"""

    def setUp(self):
        from tests.test_auth import FakeTransport
        self.tmp = make_temp_dir("remote_endpoints")
        self.db_path = self.tmp / "vps.sqlite3"
        self._orig_user_data_dir = config.USER_DATA_DIR
        config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self._remote_snapshot = (config.REMOTE_BASE, config.REMOTE_AUTH_TOKEN)
        self._sb_snapshot = (supabase_client.config.SUPABASE_PROJECT_REF,
                             supabase_client.config.SUPABASE_TOKEN,
                             supabase_client.config.STORAGE_BACKEND)
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        supabase_client.config.STORAGE_BACKEND = "cloud"   # 模拟 VPS 云模式
        config.REMOTE_BASE = ""                            # VPS 不把自己当 C 端
        config.REMOTE_AUTH_TOKEN = "vps-secret"
        self.transport = FakeTransport()
        self.transport.accounts = {"vpsuser": {
            "username": "vpsuser", "password_hash": "hash", "salt": "salt",
            "api_key": "ak", "site_origin": "https://example.net", "created_at": "2026-01-01",
        }}
        supabase_client.set_transport(self.transport)
        remote_client.reset_client()
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        config.USER_DATA_DIR = self._orig_user_data_dir
        config.REMOTE_BASE, config.REMOTE_AUTH_TOKEN = self._remote_snapshot
        (supabase_client.config.SUPABASE_PROJECT_REF,
         supabase_client.config.SUPABASE_TOKEN,
         supabase_client.config.STORAGE_BACKEND) = self._sb_snapshot
        supabase_client.set_transport(None)
        remote_client.reset_client()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remote_account_get_requires_correct_token(self):
        # 错密钥 → 403
        status, data, _ = http("GET", self.base, "/api/remote/account?username=vpsuser",
                               headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 403)
        # 对密钥 → 账号行
        status, data, _ = http("GET", self.base, "/api/remote/account?username=vpsuser",
                               headers={"Authorization": "Bearer vps-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(data["account"]["username"], "vpsuser")

    def test_remote_sync_pull_reuses_supabase(self):
        self.transport.galleries["vpsuser"] = []
        status, data, _ = http("POST", self.base, "/api/remote/sync",
                                body={"username": "vpsuser", "direction": "pull"},
                                headers={"Authorization": "Bearer vps-secret"})
        self.assertEqual(status, 200)
        self.assertIn("galleries", data)
        self.assertIn("counts", data)


if __name__ == "__main__":
    unittest.main()
