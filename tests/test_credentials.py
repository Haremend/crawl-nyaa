"""凭据读/写 + 离线（匿名）模式测试（全部离线，无网络）。

覆盖：
- 登录用户 GET /api/settings/credentials 回显明文 api_key（mode=account）
- 匿名 POST/GET /api/settings/credentials 走 offline 模式（unlogin_user 账号行）
- 匿名 GET /api/meta 站点地址回退到 unlogin_user 账号行 / settings
"""
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import json as _json

from app import config, db
from app.server import ApiServer, Handler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def _http_full(method, base, path, *, body=None, cookie=None):
    """轻量 HTTP 客户端：支持 cookie/请求体；对 4xx/5xx 也返回 (status, payload, set_cookie)。"""
    url = base + path
    data = _json.dumps(body).encode("utf-8") if body is not None else None
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
            payload = _json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        return exc.code, payload, set_cookie
    with response:
        status = response.status
        raw = response.read()
        set_cookie = response.headers.get("Set-Cookie", "")
    try:
        payload = _json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    return status, payload, set_cookie


def http(method, base, path, *, body=None, cookie=None):
    """不关心 Set-Cookie 的便捷封装。"""
    status, payload, _ = _http_full(method, base, path, body=body, cookie=cookie)
    return status, payload


def session_cookie(set_cookie_header: str) -> str:
    for part in set_cookie_header.split(";"):
        part = part.strip()
        if part.startswith("claw_session="):
            return part
    return ""


def setUpModule():
    from tests._isolated import _force_local
    _force_local()


class CredentialsTests(unittest.TestCase):
    """凭据读写：登录回显明文 Key；匿名走 offline 模式读写 unlogin_user 账号行。"""

    def setUp(self):
        self.tmp = make_temp_dir("creds")
        self.db_path = self.tmp / "creds.sqlite3"
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

    def test_logged_in_get_returns_plaintext_key(self):
        # 1) 注册带 api_key=plain-key
        status, data, sc = _http_full(
            "POST", self.base, "/api/auth/register",
            body={"username": "alice", "password": "pass1", "api_key": "plain-key"})
        self.assertEqual(status, 200)
        cookie = session_cookie(sc)
        self.assertTrue(cookie)
        # 2) 登录（create_session 会清掉旧会话，必须用登录响应里的新 cookie）
        status, data, sc = _http_full(
            "POST", self.base, "/api/auth/login",
            body={"username": "alice", "password": "pass1"}, cookie=cookie)
        self.assertEqual(status, 200)
        cookie = session_cookie(sc)
        self.assertTrue(cookie)
        # 3) GET 凭据：回显明文 api_key
        status, data = http("GET", self.base, "/api/settings/credentials", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "account")
        self.assertEqual(data["api_key"], "plain-key")
        self.assertTrue(data["has_api_key"])

    def test_offline_post_then_get_credentials(self):
        # 匿名 POST 凭据（无 cookie）
        status, data = http("POST", self.base, "/api/settings/credentials",
                            body={"api_key": "off-key", "site_origin": "example.net"})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "offline")
        self.assertEqual(data["username"], config.UNLOGIN_USER)
        # site_origin 规范化为 https://example.net
        self.assertEqual(data["site_origin"], "https://example.net")
        self.assertEqual(data["api_key"], "off-key")
        self.assertFalse(data["cloud_pushed"])  # 离线模式绝不触云

        # 匿名 GET 凭据（无 cookie）
        status, data = http("GET", self.base, "/api/settings/credentials")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "offline")
        self.assertEqual(data["username"], config.UNLOGIN_USER)
        self.assertEqual(data["api_key"], "off-key")
        self.assertEqual(data["site_origin"], "https://example.net")

        # 用户库 settings 也写入了 site_origin
        self.assertEqual(
            db.get_setting("site_origin", "",
                           db.user_db_path(config.UNLOGIN_USER)),
            "https://example.net")

    def test_anonymous_meta_returns_offline_site_origin(self):
        # 先匿名保存 site_origin
        http("POST", self.base, "/api/settings/credentials",
             body={"api_key": "off-key", "site_origin": "example.net"})
        # 匿名 GET /api/meta：site_origin 回退到 unlogin_user 账号行 / settings
        status, data = http("GET", self.base, "/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(data["site_origin"], "https://example.net")


if __name__ == "__main__":
    unittest.main()
