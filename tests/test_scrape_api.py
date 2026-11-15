"""抓取主链路走 API 的集成测试：ScrapeService + 内存版搜索接口（不触网）。

覆盖：批次记录 URL 与历史同形、条目字段入库（缩略图 t. 子域）、翻页与
max_pages、增量「遇到已收录即停」、/search 的 q 透传、第一页空结果报错、
抓取队列落在实例自己的默认库（不串到真实库）。
"""
import json
import shutil
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app import config, db
from app import scrape_service as service_module
from app.server import ApiServer, Handler
from app.upstream import UpstreamTag

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir(prefix: str) -> Path:
    path = TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def search_item(gid, title=None, tag_ids=(), blacklisted=False):
    """搜索接口单条结果（字段与真实 payload 对齐）。"""
    return {
        "id": gid, "media_id": 1000 + gid,
        "english_title": title or f"Work {gid}",
        "japanese_title": f"作品 {gid}",
        "thumbnail": f"galleries/{1000 + gid}/thumb.jpg.webp",
        "tag_ids": list(tag_ids), "blacklisted": blacklisted,
    }


def payload(items, num_pages=1):
    return {"result": items, "page": 1, "num_pages": num_pages,
            "per_page": 25, "total": len(items)}


class FakeSearch:
    """内存版 /api/v2/search：按页返回预置 payload，记录调用。"""

    def __init__(self, pages, tags=None):
        self.pages = pages            # {page_no: payload}
        self.tags = tags or {}        # tag_id -> UpstreamTag
        self.calls = []
        self.origins = []

    def search(self, query, page=1, sort=""):
        self.calls.append({"query": query, "page": page, "sort": sort})
        return self.pages.get(page) or payload([])

    def resolve_tags(self, tag_ids):
        return {tid: self.tags[tid] for tid in tag_ids if tid in self.tags}


class ScrapeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("scrape_api")
        self.db_path = self.tmp / "api.sqlite3"
        self._orig_user_dir = config.USER_DATA_DIR
        config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self._mode_snapshot = config.SCRAPE_MODE
        config.SCRAPE_MODE = "api"

    def tearDown(self):
        config.SCRAPE_MODE = self._mode_snapshot
        config.USER_DATA_DIR = self._orig_user_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------
    def _install_fake(self, pages, tags=None) -> FakeSearch:
        """替换 ScrapeService 构造的 UpstreamClient（真实 iter_pages_api 照跑）。"""
        fake = FakeSearch(pages, tags=tags)
        original = service_module.UpstreamClient

        def factory(origin, *args, **kwargs):
            fake.origins.append(origin)
            return fake

        service_module.UpstreamClient = factory
        self.addCleanup(setattr, service_module, "UpstreamClient", original)
        return fake

    def _run(self, url, *, pages, max_pages=1, tags=None, incremental=True):
        fake = self._install_fake(pages, tags=tags)
        svc = service_module.ScrapeService(self.db_path)
        svc.start(url, max_pages=max_pages, enrich=False, covers=False,
                  incremental=incremental)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap = svc.snapshot()
            if not snap["running"]:
                return snap, fake
            time.sleep(0.05)
        self.fail("抓取任务未在超时前结束")

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM galleries ORDER BY gallery_id")]
        finally:
            conn.close()

    def _run_row(self, run_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM scrape_runs WHERE run_id = ?",
                               (run_id,)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    # ---------- 用例 ----------
    def test_artist_page_scrapes_via_api_and_keeps_batch_url(self):
        url = "https://example.net/artist/sample-artist/"
        tags = {1: UpstreamTag(1, "language", "Chinese", "chinese"),
                2: UpstreamTag(2, "artist", "Sample Artist", "sample-artist")}
        pages = {1: payload([search_item(9001, tag_ids=[1, 2]),
                             search_item(9002, tag_ids=[1])])}
        snap, fake = self._run(url, pages=pages, tags=tags)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["items_new"], 2)
        # 映射成搜索条件 + 站点地址来自列表 URL
        self.assertEqual(fake.calls[0]["query"], 'artist:"sample-artist"')
        self.assertEqual(fake.origins, ["https://example.net"])
        # 入库字段
        rows = self._rows()
        self.assertEqual([r["gallery_id"] for r in rows], [9001, 9002])
        self.assertEqual(rows[0]["title"], "Work 9001")
        self.assertEqual(rows[0]["thumbnail"],
                         "https://t.example.net/galleries/10001/thumb.jpg.webp")
        self.assertIn("chinese", rows[0]["languages"])
        self.assertIn("Sample Artist", rows[0]["artists"])
        self.assertEqual(rows[0]["batch_id"], snap["run_id"])
        # 批次记录的 URL 与历史批次同形（规范化列表 URL，不含 page）
        self.assertEqual(self._run_row(snap["run_id"])["url"], url)

    def test_pagination_respects_max_pages(self):
        url = "https://example.net/group/sample/"
        pages = {1: payload([search_item(1)], num_pages=3),
                 2: payload([search_item(2)], num_pages=3),
                 3: payload([search_item(3)], num_pages=3)}
        snap, fake = self._run(url, pages=pages, max_pages=2)
        self.assertEqual(snap["status"], "done")
        self.assertEqual([c["page"] for c in fake.calls], [1, 2])
        self.assertEqual(snap["items_new"], 2)
        self.assertEqual(snap["pages_total"], 2)

    def test_incremental_stops_after_known_item(self):
        url = "https://example.net/group/sample/"
        snap1, _ = self._run(url, pages={1: payload([search_item(1), search_item(2),
                                                     search_item(3)])})
        self.assertEqual(snap1["items_new"], 3)
        second = {1: payload([search_item(4), search_item(3), search_item(2)], num_pages=2),
                  2: payload([search_item(1)], num_pages=2)}
        snap2, fake2 = self._run(url, pages=second, max_pages=2)
        self.assertEqual(snap2["status"], "done")
        self.assertEqual(snap2["items_new"], 1, "只入库新的那一条")
        self.assertEqual(len(fake2.calls), 1, "第一页遇到已收录后不应再翻页")

    def test_search_url_query_is_passed_through(self):
        url = "https://example.net/search?q=Sample+Mag+Decensored"
        snap, fake = self._run(url, pages={1: payload([search_item(5)])})
        self.assertEqual(snap["status"], "done")
        self.assertEqual(fake.calls[0]["query"], "Sample Mag Decensored")

    def test_first_page_empty_reports_readable_error(self):
        snap, _ = self._run("https://example.net/artist/nobody/",
                            pages={1: payload([])})
        self.assertEqual(snap["status"], "error")
        self.assertIn("没有作品", snap["error"] or "")

    def test_blacklisted_items_never_enter_db(self):
        url = "https://example.net/tag/sample/"
        pages = {1: payload([search_item(11), search_item(12, blacklisted=True)])}
        snap, _ = self._run(url, pages=pages)
        self.assertEqual(snap["items_new"], 1)
        self.assertEqual([r["gallery_id"] for r in self._rows()], [11])


class ScrapeQueueIsolationTests(unittest.TestCase):
    """scrape_jobs 是实例级队列，任务行必须落在实例自己的默认库里。

    历史缺陷：`Handler._scrape_service` 构造 `ScrapeService` 时没传 db_path，
    队列回退到 `config.DB_PATH`（真实默认库）——用临时库起的服务（测试/多实例）
    会读写真实库的队列：启动时「重启恢复」捡走别的实例残留的任务执行，还会把
    它们的状态改成 cancelled。
    """

    def setUp(self):
        self.tmp = make_temp_dir("queue_iso")
        self.db_path = self.tmp / "iso.sqlite3"
        self._orig_user_dir = config.USER_DATA_DIR
        config.USER_DATA_DIR = self.tmp / "users"
        db.init_db(self.db_path)
        self._mode_snapshot = config.SCRAPE_MODE
        config.SCRAPE_MODE = "api"
        fake = FakeSearch({1: payload([search_item(777)])})
        original = service_module.UpstreamClient
        service_module.UpstreamClient = lambda origin, *a, **k: fake
        self.addCleanup(setattr, service_module, "UpstreamClient", original)
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        config.SCRAPE_MODE = self._mode_snapshot
        config.USER_DATA_DIR = self._orig_user_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post_json(self, path, body):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw.decode("utf-8")) if raw else {})

    def _scrape_job_urls(self, path):
        conn = sqlite3.connect(path)
        try:
            return [str(r[0]) for r in conn.execute("SELECT url FROM scrape_jobs")]
        except sqlite3.OperationalError:      # 库还没建表
            return []
        finally:
            conn.close()

    def test_job_row_lands_in_instance_db_not_real_db(self):
        url = "https://example.net/artist/queue-isolation-check/"
        status, data = self._post_json("/api/scrape", {"url": url, "max_pages": 1})
        self.assertEqual(status, 200, data)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._scrape_job_urls(self.db_path):
                break
            time.sleep(0.05)
        self.assertTrue(any("queue-isolation-check" in u
                            for u in self._scrape_job_urls(self.db_path)),
                        "任务行必须写进实例自己的默认库")
        real_db = Path(config.DB_PATH)
        if real_db.exists():
            self.assertFalse(any("queue-isolation-check" in u
                                 for u in self._scrape_job_urls(real_db)),
                             "任务行绝不能写进真实默认库（config.DB_PATH）")


if __name__ == "__main__":
    unittest.main()
