"""配置生效性测试：.env 里配的每个抓取参数必须真的影响实际行为。

背景（用户反馈）：`.env` 里把「页面间隔」配成 0.1，页面上仍显示 1.2、
实际也按 1.2/0.2 走 —— 即「配了却不生效」。本文件锁住三类回归：

1. `CLAW_DELAY` 配多少就是多少（不会被硬编码的 0.2 下限悄悄改掉）。
2. `/api/meta` 的 limits 由 config 驱动（前端表单默认值/上下限的来源）。
3. `CLAW_USER_DATA_DIR` 真的决定用户库目录（历史 bug：写死在 DATA_DIR/users）。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path

from app import config, db
from app.server import ApiServer, Handler

from tests._isolated import _force_local

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    # 不用 tempfile.TemporaryDirectory：它创建 0o700 目录，受限环境下连自己都进不去。
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


class PageDelayClampTests(unittest.TestCase):
    """页面间隔的夹取规则：.env 配的值一定生效，请求传 0 仍被拦住。"""

    def setUp(self):
        self._snapshot = (config.PAGE_DELAY, config.MIN_PAGE_DELAY, config.MAX_PAGE_DELAY)

        def restore():
            config.PAGE_DELAY, config.MIN_PAGE_DELAY, config.MAX_PAGE_DELAY = self._snapshot

        self.addCleanup(restore)

    def test_configured_delay_below_default_floor_is_honored(self):
        """CLAW_DELAY=0.1：下限取 min(0.2, 0.1)=0.1，所以 0.1 原样生效。

        历史 bug：`max(MIN_PAGE_DELAY, delay)` 会把 0.1 抬到 0.2，
        用户改了 .env 却看不出区别。
        """
        config.PAGE_DELAY = 0.1
        config.MIN_PAGE_DELAY = 0.2
        self.assertEqual(config.clamp_page_delay(0.1), 0.1)

    def test_zero_still_clamped_to_floor(self):
        """请求里传 0（或负数）仍会被下限拦住，避免把上游打满。"""
        config.PAGE_DELAY = 1.2
        config.MIN_PAGE_DELAY = 0.2
        self.assertEqual(config.clamp_page_delay(0), 0.2)
        self.assertEqual(config.clamp_page_delay(-5), 0.2)

    def test_configured_delay_above_default_ceiling_is_honored(self):
        """CLAW_DELAY=30：上限取 max(10, 30)=30，所以 30 不会被压回 10。"""
        config.PAGE_DELAY = 30.0
        config.MAX_PAGE_DELAY = 10.0
        self.assertEqual(config.clamp_page_delay(30), 30.0)

    def test_ceiling_still_applies_to_larger_request(self):
        config.PAGE_DELAY = 1.2
        config.MAX_PAGE_DELAY = 10.0
        self.assertEqual(config.clamp_page_delay(999), 10.0)

    def test_bounds_always_contain_configured_delay(self):
        """用户场景：CLAW_DELAY=0.1 而 CLAW_MIN_DELAY 仍是默认 0.2。

        实际下限要跟着变成 0.1，否则页面上「间隔=0.1」会被自己的 min 判为非法。
        """
        config.PAGE_DELAY = 0.1
        config.MIN_PAGE_DELAY = 0.2
        config.MAX_PAGE_DELAY = 10.0
        self.assertEqual(config.page_delay_bounds(), (0.1, 10.0))


class EnvOverrideTests(unittest.TestCase):
    """端到端：子进程里设 CLAW_DELAY=0.1，config 读到的就是 0.1 且夹取后仍是 0.1。

    用子进程而不是 importlib.reload，避免污染本进程的全局 config。
    """

    def test_env_delay_reaches_config_and_clamp(self):
        script = (
            "import json; from app import config;"
            "print(json.dumps({'page_delay': config.PAGE_DELAY,"
            " 'min_delay': config.MIN_PAGE_DELAY,"
            " 'clamped': config.clamp_page_delay(config.PAGE_DELAY)}))"
        )
        env = dict(os.environ)
        env["CLAW_DELAY"] = "0.1"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT), env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertAlmostEqual(data["page_delay"], 0.1)
        self.assertAlmostEqual(data["clamped"], 0.1)

    def test_env_min_delay_is_configurable(self):
        script = (
            "import json; from app import config;"
            "print(json.dumps({'min_delay': config.MIN_PAGE_DELAY,"
            " 'clamped_zero': config.clamp_page_delay(0)}))"
        )
        env = dict(os.environ)
        env["CLAW_DELAY"] = "1.2"
        env["CLAW_MIN_DELAY"] = "0.05"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT), env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertAlmostEqual(data["min_delay"], 0.05)
        self.assertAlmostEqual(data["clamped_zero"], 0.05)


class UserDataDirTests(unittest.TestCase):
    """CLAW_USER_DATA_DIR 必须真的决定用户库位置（否则测试会污染 data/users）。"""

    def setUp(self):
        self._orig = config.USER_DATA_DIR
        self.addCleanup(setattr, config, "USER_DATA_DIR", self._orig)
        self.tmp = make_temp_dir("userdir")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_user_data_dir_is_used_without_double_nesting(self):
        config.USER_DATA_DIR = self.tmp / "users"
        path = db.user_db_path("alice")
        self.assertEqual(path, self.tmp / "users" / "alice.sqlite3")

    def test_base_dir_argument_still_means_data_root(self):
        """base_dir 是「数据根目录」，用户库放在它下面的 users/。"""
        root = self.tmp / "root"
        self.assertEqual(db.user_db_path("bob", root), root / "users" / "bob.sqlite3")

    def test_default_matches_historical_layout(self):
        """默认值仍是 <DATA_DIR>/users/<name>.sqlite3，老部署不受影响。"""
        config.USER_DATA_DIR = config.DATA_DIR / "users"
        self.assertEqual(db.user_db_path("carol"), config.DATA_DIR / "users" / "carol.sqlite3")


class MetaLimitsTests(unittest.TestCase):
    """/api/meta 的 limits 必须来自 config（前端表单默认值/上下限的唯一来源）。"""

    @classmethod
    def setUpClass(cls):
        _force_local()
        cls.tmp = make_temp_dir("metalimits")
        cls.db_path = cls.tmp / "meta.sqlite3"
        db.init_db(cls.db_path)
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

    def _limits(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/meta", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))["limits"]

    def test_limits_reflect_config(self):
        snapshot = (config.PAGE_DELAY, config.MIN_PAGE_DELAY, config.MAX_PAGE_DELAY,
                    config.MAX_PAGES, config.ENRICH_DEFAULT, config.COVER_DEFAULT,
                    config.INCREMENTAL)
        try:
            config.PAGE_DELAY = 0.1
            config.MIN_PAGE_DELAY = 0.1
            config.MAX_PAGE_DELAY = 3.0
            config.MAX_PAGES = 42
            config.ENRICH_DEFAULT = False
            config.COVER_DEFAULT = False
            config.INCREMENTAL = False
            limits = self._limits()
            self.assertAlmostEqual(limits["default_delay"], 0.1)
            self.assertAlmostEqual(limits["min_delay"], 0.1)
            self.assertAlmostEqual(limits["max_delay"], 3.0)
            self.assertEqual(limits["max_pages"], 42)
            self.assertFalse(limits["enrich_default"])
            self.assertFalse(limits["covers_default"])
            self.assertFalse(limits["incremental_default"])
        finally:
            (config.PAGE_DELAY, config.MIN_PAGE_DELAY, config.MAX_PAGE_DELAY,
             config.MAX_PAGES, config.ENRICH_DEFAULT, config.COVER_DEFAULT,
             config.INCREMENTAL) = snapshot

    def test_min_delay_reports_effective_floor(self):
        """用户场景：.env 里 CLAW_DELAY=0.1、CLAW_MIN_DELAY 没配（默认 0.2）。

        /api/meta 的 min_delay 必须是 0.1（实际下限），否则前端输入框
        的 min 属性会把预填的 0.1 判为非法值。
        """
        snapshot = (config.PAGE_DELAY, config.MIN_PAGE_DELAY)
        try:
            config.PAGE_DELAY = 0.1
            config.MIN_PAGE_DELAY = 0.2
            limits = self._limits()
            self.assertAlmostEqual(limits["default_delay"], 0.1)
            self.assertAlmostEqual(limits["min_delay"], 0.1)
        finally:
            config.PAGE_DELAY, config.MIN_PAGE_DELAY = snapshot

    def test_limits_keys_present(self):
        """前端 applyLimits() 依赖这些键，缺一个就退回硬编码默认值。"""
        limits = self._limits()
        for key in ("max_pages", "default_delay", "min_delay", "max_delay",
                    "detail_delay", "cover_delay",
                    "enrich_default", "covers_default", "incremental_default"):
            self.assertIn(key, limits, f"limits 缺少 {key}")


class FrontendWiringTests(unittest.TestCase):
    """前端不得再硬编码这些默认值 —— 必须从 /api/meta 的 limits 取。"""

    def test_apply_limits_exists_and_is_called(self):
        js = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function applyLimits(", js)
        self.assertIn("applyLimits(meta)", js, "loadMeta 应套用 limits")
        for element_id in ("delay", "maxPages", "enrichCheck", "coverCheck", "incrementalCheck"):
            self.assertIn('$("%s")' % element_id, js, f"applyLimits 应初始化 {element_id}")

    def test_delay_input_has_no_hardcoded_bounds(self):
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="delay" type="number" min="0.2" max="10"', html,
                         "delay 的 min/max 应由 limits 决定，不能硬编码")
        self.assertIn('id="incrementalCheck"', html, "增量抓取应有开关（否则 CLAW_INCREMENTAL 无效）")

    def test_scrape_request_uses_checkbox_states(self):
        js = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('incremental: $("incrementalCheck").checked', js,
                      "抓取请求应带上增量开关的真实状态，不能写死 true")


if __name__ == "__main__":
    unittest.main()
