"""伪语言标签污染 primary_language 修复的回归测试（全部离线）。

设计（设计 B — 边界清洗）：不在每个源解析器过滤，而是在所有写入 galleries.languages
/ primary_language 的持久化边界做统一清洗，保证「任何来源入库的数据都干净」。
涉及 4 个写点：
  - app/dao/galleries.py       _params_from_row    （本地 upsert）
  - app/dao/galleries.py       apply_details       （本地详情合并）
  - app/scrape_service.py      CloudSink._row_from_item （云端入库）
  - app/scrape_service.py      CloudSink.apply_details  （云端详情合并 ← 本次补充 primary_language 重算）
以及两次一次性修复：
  - app/dao/schema.py          self_heal_languages  （本地启动自愈，run.py 调用）
  - app/supabase_client.py     cloud_repair_languages   （云端手动修复）

所有测试不触及真实网络；Supabase 走 FakeTransport 桩。
"""
import json
import shutil
import unittest
import uuid
from pathlib import Path

from app import config, db, supabase_client
from app.parser import GalleryItem
from app.detail import parse_detail_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


# ============================================================
# 1. config.clean_languages 单元
# ============================================================

class CleanLanguagesTests(unittest.TestCase):
    def test_drops_pseudo_keeps_real(self):
        self.assertEqual(config.clean_languages(["translated", "chinese"]), ["chinese"])
        self.assertEqual(config.clean_languages(["speechless", "japanese", "text cleaned"]),
                         ["japanese"])
        self.assertEqual(config.clean_languages(["rewrite", "english"]), ["english"])
        # 全伪
        self.assertEqual(config.clean_languages(["translated", "speechless"]), [])
        # 大小写与去重
        self.assertEqual(config.clean_languages(["Translated", "translated", "chinese"]),
                         ["chinese"])

    def test_none_and_blank_are_safe(self):
        self.assertEqual(config.clean_languages(None), [])
        self.assertEqual(config.clean_languages([]), [])
        self.assertEqual(config.clean_languages([None, "", "chinese"]), ["chinese"])


# ============================================================
# 2. 本地持久化边界
# ============================================================

class LocalUpsertCleanTests(unittest.TestCase):
    """db.upsert_items：入库收敛点 1 —— 列表/收藏/同步 数据落库时必清洗。"""

    def setUp(self):
        self.tmp = make_temp_dir("local_upsert")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_translated_filtered_and_primary_computed(self):
        item = GalleryItem(gallery_id=1, url="https://example.net/g/1/",
                           title="W", languages=["translated", "chinese"])
        db.upsert_items([item], source_url="demo", path=self.db_path)
        conn = db.connect(self.db_path)
        row = conn.execute(
            "SELECT languages, primary_language FROM galleries WHERE gallery_id=1").fetchone()
        conn.close()
        self.assertEqual(json.loads(row["languages"]), ["chinese"])
        self.assertEqual(row["primary_language"], "chinese")


class LocalApplyDetailsCleanTests(unittest.TestCase):
    """db.apply_details：入库收敛点 2 —— 详情补全写回时必清洗。"""

    def setUp(self):
        self.tmp = make_temp_dir("local_apply")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detail_merge_filters_pseudo_and_syncs_primary(self):
        # 先上库一行（列表抓到的脏 languages）
        db.upsert_items(
            [GalleryItem(gallery_id=2, url="https://example.net/g/2/", title="W",
                        languages=["translated", "chinese"])],
            source_url="demo", path=self.db_path)
        # 详情 API 返回：只认 chinese（detail.parse_detail_payload 已过滤 pseudo）
        detail = parse_detail_payload({
            "id": 2,
            "title": {"english": "W", "japanese": "サンプル", "pretty": "W"},
            "tags": [{"type": "language", "name": "Chinese", "slug": "chinese"}],
        })
        self.assertEqual(detail.languages, ["chinese"], "详情解析本身已经过过滤")
        db.apply_details([detail], self.db_path)
        conn = db.connect(self.db_path)
        row = conn.execute(
            "SELECT languages, primary_language FROM galleries WHERE gallery_id=2").fetchone()
        conn.close()
        self.assertEqual(json.loads(row["languages"]), ["chinese"])
        self.assertEqual(row["primary_language"], "chinese")


# ============================================================
# 3. 云端持久化边界（CloudSink）
# ============================================================

class CloudSinkRowTests(unittest.TestCase):
    """CloudSink._row_from_item：入库收敛点 3 —— 抓取/收藏 落云时必清洗。"""

    def test_row_filters_pseudo_tags(self):
        from app.scrape_service import CloudSink
        sink = CloudSink("alice")
        item = GalleryItem(gallery_id=1, url="https://example.net/g/1/",
                           title="W", languages=["translated", "chinese"])
        row = sink._row_from_item(item, source_url="demo", now="x", batch_id=None)
        self.assertEqual(row["languages"], '["chinese"]')
        self.assertEqual(row["primary_language"], "chinese")


class CloudApplyDetailsFixesPrimaryTests(unittest.TestCase):
    """CloudSink.apply_details：本次新增 —— 补全时不再漏掉 primary_language 重算。

    历史 bug（就是产生云端 222 行 dirty 的成因）：apply_details 循环里把
    `raw.get("languages")` 写到 base["languages"]，却没有顺带把
    `base["primary_language"]` 重算成 `languages[0]`。当详情只带真实语言
    （无 translated）而 pending 的原始列表里有 translated 时，就产出了
    `languages=["chinese"]` 但 `primary_language="translated"` 的半脏行。
    """

    def test_applies_details_syncs_primary_to_cleaned_first_lang(self):
        from app.scrape_service import CloudSink
        from tests.test_auth import FakeTransport
        # apply_details 结尾会 push_user：装 FakeTransport 保持离线
        sb_ref, sb_token = (supabase_client.config.SUPABASE_PROJECT_REF,
                            supabase_client.config.SUPABASE_TOKEN)
        transport = FakeTransport()
        supabase_client.set_transport(transport)
        self.addCleanup(supabase_client.set_transport, None)
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        self.addCleanup(setattr, supabase_client.config, "SUPABASE_PROJECT_REF", sb_ref)
        self.addCleanup(setattr, supabase_client.config, "SUPABASE_TOKEN", sb_token)

        sink = CloudSink("bob")
        # 模拟一次已经入库的作品（pending 里已有脏 primary）
        sink._pending[99] = {
            "gallery_id": 99, "last_seen": "2026-01-01T00:00:00+00:00",
            "languages": json.dumps(["translated", "chinese"]),
            "primary_language": "translated",   # 脏：被 translated 顶掉
        }
        detail = parse_detail_payload({
            "id": 99,
            "title": {"english": "W", "japanese": "サンプル", "pretty": "W"},
            "tags": [{"type": "language", "name": "Chinese", "slug": "chinese"}],
        })
        count = sink.apply_details([detail])
        self.assertEqual(count, 1)
        final = sink._pending[99]
        self.assertEqual(json.loads(final["languages"]), ["chinese"])
        self.assertEqual(final["primary_language"], "chinese",
                         "primary_language 必须与清洗后的 languages[0] 保持一致")
        self.assertTrue(final["detail_fetched_at"])


# ============================================================
# 4. 启动自愈（本地存量清洗）
# ============================================================

class SelfHealLanguagesTests(unittest.TestCase):
    """db.self_heal_languages：启动幂等修复存量库，已清洗的行不重复写。"""

    def _dirty_db(self, prefix):
        path = make_temp_dir(prefix) / "dirty.sqlite3"
        db.init_db(path)
        conn = db.connect(path)
        now = "2026-01-01T00:00:00+00:00"
        for gid, langs, primary in [
            (690038, '["translated", "chinese"]', "translated"),   # 真实场景 346036
            (690041, '["english", "translated"]', "english"),       # primary 干净但混入 list
            (691307, "[]", "translated"),                           # 空 list + 伪主语言
            (700000, '["japanese"]', "japanese"),                   # 干净的不动
        ]:
            conn.execute(
                "INSERT INTO galleries (gallery_id, url, languages, primary_language, "
                "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, f"/g/{gid}/", langs, primary, now, now))
        conn.commit()
        conn.close()
        return path

    def test_heals_and_is_idempotent(self):
        path = self._dirty_db("heal")
        first = db.self_heal_languages(path)
        self.assertEqual(first, 3, "三条脏行都应被修复")
        conn = db.connect(path)
        by_id = {r["gallery_id"]: r for r in conn.execute(
            "SELECT gallery_id, languages, primary_language FROM galleries")}
        conn.close()
        self.assertEqual(json.loads(by_id[690038]["languages"]), ["chinese"])
        self.assertEqual(by_id[690038]["primary_language"], "chinese")
        self.assertEqual(json.loads(by_id[690041]["languages"]), ["english"])
        self.assertEqual(by_id[690041]["primary_language"], "english")
        self.assertEqual(json.loads(by_id[691307]["languages"]), [])
        self.assertEqual(by_id[691307]["primary_language"], "unknown")
        # 干净的保持原样
        self.assertEqual(json.loads(by_id[700000]["languages"]), ["japanese"])
        self.assertEqual(by_id[700000]["primary_language"], "japanese")
        # 幂等：再跑一次不改动
        self.assertEqual(db.self_heal_languages(path), 0)

    def test_clean_db_untouched(self):
        path = make_temp_dir("clean") / "clean.sqlite3"
        db.init_db(path)
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO galleries (gallery_id, url, languages, primary_language, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "/g/1/", '["chinese"]', "chinese", "x", "x"))
        conn.commit()
        conn.close()
        self.assertEqual(db.self_heal_languages(path), 0, "干净库不应被改写")


# ============================================================
# 5. 云端一次性修复（FakeTransport 离线验证）
# ============================================================

class CloudRepairLanguageTests(unittest.TestCase):
    """supabase_client.cloud_repair_languages：用内存 FakeTransport 离线验证逻辑。"""

    def _make_sink(self):
        from tests.test_auth import FakeTransport
        self._sb_snapshot = (supabase_client.config.SUPABASE_PROJECT_REF,
                             supabase_client.config.SUPABASE_TOKEN)
        supabase_client.config.SUPABASE_PROJECT_REF = "test-project"
        supabase_client.config.SUPABASE_TOKEN = "test-token"
        transport = FakeTransport()
        supabase_client.set_transport(transport)
        return transport

    def _restore(self):
        (supabase_client.config.SUPABASE_PROJECT_REF,
         supabase_client.config.SUPABASE_TOKEN) = self._sb_snapshot
        supabase_client.set_transport(None)

    def _install_scan_stub(self, transport, rows):
        original = transport.query

        def query(sql, *, read_only=False):
            original(sql, read_only=read_only)
            low = sql.lower()
            if (low.startswith("select") and "primary_language" in low
                    and "from public" in low):
                return list(rows)
            return []

        transport.query = query
        return transport

    def test_dry_run_counts_without_writing(self):
        transport = self._make_sink()
        self._install_scan_stub(transport, [
            {"username": "u", "gallery_id": 1,
             "languages": '["chinese"]', "primary_language": "translated"},
        ])
        result = supabase_client.cloud_repair_languages("u")
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["would_fix"], 1, "干跑应报告需修复 1 行")
        self.assertEqual(result["fixed"], 0, "干跑不得写库")
        self.assertFalse(result["applied"])
        updates = [s for s in transport.statements if s.lower().startswith("update")]
        self.assertEqual(updates, [], "干跑不能发 UPDATE")
        self._restore()

    def test_apply_issues_case_when_update(self):
        transport = self._make_sink()
        self._install_scan_stub(transport, [
            {"username": "u", "gallery_id": 1,
             "languages": '["chinese"]', "primary_language": "translated"},
        ])
        result = supabase_client.cloud_repair_languages("u", apply=True)
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["would_fix"], 1)
        self.assertEqual(result["fixed"], 1, "apply 后 fixed 等于实发 UPDATE 行数")
        self.assertTrue(result["applied"])
        updates = [s for s in transport.statements if s.lower().startswith("update")]
        self.assertEqual(len(updates), 1)
        self.assertIn('"languages" = case', updates[0].lower())
        self._restore()

    def test_no_update_when_clean(self):
        # LIKE 命中子串但清洗后无变化（primary 本身是合法语言）-> 需修复 0
        transport = self._make_sink()
        self._install_scan_stub(transport, [
            {"username": "u", "gallery_id": 2,
             "languages": '["chinese"]', "primary_language": "chinese"},
        ])
        result = supabase_client.cloud_repair_languages("u", apply=True)
        self.assertEqual(result["would_fix"], 0, "干净行不应被标记为需修复")
        self.assertEqual(result["fixed"], 0)
        updates = [s for s in transport.statements if s.lower().startswith("update")]
        self.assertEqual(updates, [], "无变化时不发 UPDATE")
        self._restore()


if __name__ == "__main__":
    unittest.main()
