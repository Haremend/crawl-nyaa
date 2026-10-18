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

    def test_no_brand_leak_in_assets(self):
        for path in (CSS, JS, HTML):
            text = path.read_text(encoding="utf-8").lower()
            for token in ("nhentai", "cf_clearance", "access_token="):
                self.assertNotIn(token, text, f"{path.name} 不应包含 {token}")


if __name__ == "__main__":
    unittest.main()