"""前端静态资源检查：样式与脚本里的关键约定（纯静态，不需要浏览器）。"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSS = PROJECT_ROOT / "web" / "style.css"
JS = PROJECT_ROOT / "web" / "app.js"
HTML = PROJECT_ROOT / "web" / "index.html"


class StyleGuardTests(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")

    def test_hidden_attribute_takes_effect(self):
        """[hidden] 必须有 display:none，否则会被 .gate{display:flex} 之类规则覆盖。

        历史 bug：首次进入输入 API Key 后弹层不消失，就是因为缺这条规则。
        """
        self.assertRegex(
            self.css, r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important",
            "[hidden] 必须有 display:none !important，否则 hidden 属性会被 display 规则覆盖",
        )

    def test_elements_toggled_by_hidden_are_covered(self):
        """凡是用 hidden 属性控制显隐、且自身设置了 display 的类，都必须被上面的规则兜住。"""
        js = JS.read_text(encoding="utf-8")
        hidden_ids = set(re.findall(r'\$\("(\w+)"\)\.hidden\s*=', js))
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("gate", hidden_ids, "门禁层应通过 hidden 属性控制")
        # 这些 id 在页面里存在
        for element_id in hidden_ids:
            self.assertIn('id="%s"' % element_id, html, f"页面缺少 id={element_id}")

    def test_gate_can_be_hidden(self):
        """门禁层自身用了 display:flex，因此依赖 [hidden] 兜底规则生效。"""
        gate_block = re.search(r"\.gate\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(gate_block, "未找到 .gate 样式")
        self.assertIn("display: flex", gate_block.group(1).replace("\n", " "))
        self.assertIn("display: none !important", self.css)


class ScriptGuardTests(unittest.TestCase):
    def setUp(self):
        self.js = JS.read_text(encoding="utf-8")

    def test_api_key_sent_via_header(self):
        self.assertIn("X-API-Key", self.js, "前端需通过 X-API-Key 头传递 Key")

    def test_key_stored_in_cookie(self):
        self.assertIn("claw_api_key", self.js, "Key 应保存在浏览器 cookie")

    def test_all_dynamic_text_uses_esc(self):
        """卡片模板里的动态字段都应经过 esc()，避免 XSS。"""
        for field in ("item.title", "item.title_original"):
            self.assertIn("esc(" + field + ")", self.js, f"{field} 应使用 esc() 转义")

    def test_proxy_toggle_wired(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="proxyBtn"', html, "顶部应有代理开关按钮")
        self.assertIn("/api/settings/proxy", self.js, "代理开关应调用后端接口")

    def test_loading_overlay_wired(self):
        """所有后端请求都应经过 api()，由它统一控制全局加载遮罩。"""
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="loadingOverlay"', html, "页面应有全局加载遮罩")
        self.assertIn('id="loadingText"', html, "遮罩应有提示文案节点")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".loading-overlay", css, "遮罩应有样式")
        self.assertIn("display: flex", css)
        for fn in ("function showLoading(", "function hideLoading("):
            self.assertIn(fn, self.js, f"缺少 {fn}")
        self.assertIn("showLoading(options.loadingText", self.js, "api() 应在请求前显示遮罩")
        self.assertIn("if (!silent) hideLoading();", self.js, "api() 应在请求后隐藏遮罩")

    def test_polling_requests_are_silent(self):
        """轮询接口不能弹遮罩，否则每秒闪一次。"""
        self.assertIn('api("/api/scrape/status", { silent: true })', self.js)
        self.assertIn('api("/api/download/jobs?limit=60", { silent: true })', self.js)

    def test_gate_does_not_validate_on_submit(self):
        """门禁提交时不得发起校验请求。

        历史 bug：全新部署还没有站点地址，提交 Key 时校验 /api/site-favorites
        会返回「未知站点地址」，导致第一次根本进不去。
        """
        start = self.js.index("async function submitGate")
        end = self.js.index("function esc(", start)
        body = self.js[start:end]
        self.assertNotIn("fetch(", body, "submitGate 里不应有网络请求")
        self.assertNotIn("site-favorites", body, "submitGate 不应校验 Key")
        self.assertIn("writeCookie", body, "submitGate 应把 Key 存进 cookie")
        self.assertIn("hideGate()", body, "submitGate 应关闭门禁")

    def test_setup_hint_for_first_deploy(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="setupHint"', html, "应有首次部署引导提示")
        self.assertIn("renderSetupHint", self.js)

    def test_download_uses_server_relay(self):
        """卡片下载必须走本服务的转发接口，不能把上游签名地址直接交给浏览器。"""
        self.assertIn("/api/download/file", self.js, "应通过后端转发下载")
        self.assertNotIn("link.href = data.url", self.js,
                         "不应把上游地址直接赋给 download 链接（跨域会跳转且签名失效）")

    def test_redownload_switch_wired(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="redownloadCheck"', html, "应有「重新下载」开关")
        self.assertIn("redownloadEnabled()", self.js)
        self.assertIn("redownload:", self.js, "请求里应带上 redownload")

    def test_download_queue_wired(self):
        """下载必须走服务端队列：入队 + 轮询队列状态。"""
        self.assertIn("/api/download/enqueue", self.js, "应立即下载应入队")
        self.assertIn("/api/download/jobs", self.js, "应轮询队列状态")
        self.assertIn("/api/download/jobs/cancel", self.js, "应支持取消任务")
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('id="queueList"', html, "应有队列列表容器")
        self.assertIn('id="queueCancelAllBtn"', html, "应有取消排队按钮")

    def test_no_credential_leak_in_assets(self):
        """前端资源里不应出现凭据相关的字样。

        品牌词**故意不写在这里** —— 写了就等于把站点名写进仓库，
        与 AGENTS.md 第 1.1 条冲突。品牌自查请按 AGENTS.md §9 手动执行。
        """
        for path in (CSS, JS, HTML):
            text = path.read_text(encoding="utf-8").lower()
            for token in ("cf_clearance", "access_token=", "refresh_token="):
                self.assertNotIn(token, text, f"{path.name} 不应包含 {token}")


if __name__ == "__main__":
    unittest.main()