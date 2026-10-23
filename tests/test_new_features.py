"""新功能测试：隐藏（逻辑删除）、增量抓取、收藏同步。"""
import json
import shutil
import threading
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

from app import db, server as server_module, scrape_service as service_module
from app.server import ApiServer, Handler
from app.parser import GalleryItem
from app.scraper import PageResult

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
    from app import config
    return db.user_db_path(config.UNLOGIN_USER, base_dir=db_path.parent)


def item(gid=1001, title="Work A", language="japanese", **extra):
    return GalleryItem(gallery_id=gid, url=f"https://example.net/g/{gid}/",
                       title=title, languages=[language], **extra)


class HiddenDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("hidden")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hidden_filters_from_works(self):
        db.upsert_items([item(1, title="Work One"), item(2, title="Work Two")],
                        source_url="https://example.net/group/a/",
                        batch_id=1, path=self.db_path)
        self.assertEqual(db.query_works(path=self.db_path, per_page=50)["total"], 2)

        db.set_hidden([1], True, self.db_path)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["gallery_id"], 2, "隐藏的作品不应出现")

    def test_hidden_excludes_from_download_targets(self):
        db.upsert_items([item(1), item(2)], source_url="https://example.net/group/a/",
                        batch_id=1, path=self.db_path)
        db.set_want_download([1, 2], True, self.db_path)
        db.set_hidden([1], True, self.db_path)

        targets = db.list_want_download_targets(self.db_path)
        ids = [t["gallery_id"] for t in targets]
        self.assertNotIn(1, ids, "隐藏作品不应出现在待下载列表")
        self.assertIn(2, ids)

    def test_hidden_keeps_original_record_on_reupsert(self):
        """隐藏的作品再次被抓到时不覆盖、不展示。"""
        db.upsert_items([item(1)], source_url="https://example.net/group/a/",
                        batch_id=1, path=self.db_path)
        db.set_hidden([1], True, self.db_path)
        result = db.upsert_items([item(1, title="Same")],
                                 source_url="https://example.net/group/a/",
                                 batch_id=2, path=self.db_path)
        self.assertEqual(result["new"], 0, "已收录（即使隐藏）不应新增")
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertEqual(data["total"], 0, "隐藏后不应展示")


class IncrementalScrapeTests(unittest.TestCase):
    """增量抓取：同一 URL 已有成功批次时，遇到整页已收录即停止。"""

    def setUp(self):
        self.tmp = make_temp_dir("incremental")
        self.db_path = self.tmp / "t.sqlite3"
        self.db_path.parent.mkdir(exist_ok=True)
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_iter_pages(self, pages):
        """把 iter_pages 替换成预设页列表。"""
        original = service_module.iter_pages
        service_module.iter_pages = lambda url, **_kw: iter(pages)
        self.addCleanup(setattr, service_module, "iter_pages", original)

    def _run_scrape(self, items_pages, url="https://example.net/group/a/", enrich=False):
        pages = []
        for idx, items in enumerate(items_pages, start=1):
            pages.append(PageResult(
                page_no=idx, url=url + f"?page={idx}",
                items=items, last_page=len(items_pages),
                has_next=idx < len(items_pages),
            ))
        self._fake_iter_pages(pages)
        svc = service_module.ScrapeService(self.db_path)
        svc.start(url, max_pages=len(pages), enrich=enrich, covers=False, incremental=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap = svc.snapshot()
            if not snap["running"]:
                return snap
            time.sleep(0.05)
        self.fail("抓取任务未在超时前结束")

    def test_first_full_then_incremental_stops(self):
        # 第一次：完整抓到 10 条
        snap = self._run_scrape([
            [item(i, f"W{i}") for i in range(1, 6)],
            [item(i, f"W{i}") for i in range(6, 11)],
        ])
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["items_new"], 10)
        # 第二次：只有 1 条新数据，之后立刻遇到已收录页就停
        snap2 = self._run_scrape([
            [item(11, "New"), item(10, "W10"), item(9, "W9")],
            [item(8, "W8"), item(7, "W7")],
        ])
        self.assertEqual(snap2["status"], "done")
        self.assertEqual(snap2["items_new"], 1, "增量抓取应只入库新增数据")
        self.assertGreaterEqual(snap2["pages_done"], 1, "至少处理了第一页")
        self.assertLess(snap2["items_seen"], 5, "不应处理后续大量旧数据页")

    def test_existing_records_are_not_enriched_again(self):
        """再次抓取时只为新增 ID 补详情，不触碰数据库里已有的记录。"""
        url = "https://example.net/group/a/"
        old_run = db.create_run(url, self.db_path)
        db.upsert_items([item(1, "Old")], source_url=url, batch_id=old_run, path=self.db_path)
        db.finish_run(old_run, status="done", items_new=1, items_seen=1, path=self.db_path)

        calls = []
        original = service_module.fetch_detail
        original_delay = service_module.config.DETAIL_DELAY

        def fake_detail(gallery_id, *_args, **_kwargs):
            calls.append(int(gallery_id))
            return {"gallery_id": int(gallery_id), "title_pretty": f"Pretty {gallery_id}",
                    "title_original": f"Original {gallery_id}", "languages": ["japanese"],
                    "tags": [], "num_pages": 10}

        service_module.fetch_detail = fake_detail
        service_module.config.DETAIL_DELAY = 0
        self.addCleanup(setattr, service_module, "fetch_detail", original)
        self.addCleanup(setattr, service_module.config, "DETAIL_DELAY", original_delay)

        snap = self._run_scrape([[item(2, "New"), item(1, "Old")]], enrich=True)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(calls, [2], "旧记录不应再次调用详情接口或写库")

    def test_empty_batch_does_not_create_run(self):
        run_id = db.create_run("https://example.net/group/a/", self.db_path)
        db.upsert_items([item(1)], source_url="https://example.net/group/a/",
                        batch_id=run_id, path=self.db_path)
        db.finish_run(run_id, status="done", items_new=1, items_seen=1,
                      path=self.db_path)
        # 第二次抓取只拿到一条已收录数据：新增 0，批次号不应保留
        self._fake_iter_pages([PageResult(
            page_no=1, url="https://example.net/group/a/",
            items=[item(1)], last_page=1, has_next=False)])
        svc = service_module.ScrapeService(self.db_path)
        svc.start("https://example.net/group/a/", max_pages=1, enrich=False,
                  covers=False, incremental=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and svc.snapshot()["running"]:
            time.sleep(0.05)
        runs = db.list_batches(self.db_path)
        self.assertEqual(len(runs), 1, "空批次号不应保留")
        self.assertEqual(runs[0]["run_id"], run_id, "应保留第一个真实批次")
        self.assertNotEqual(svc.snapshot()["run_id"], run_id, "第二次抓取应生成过临时批次号")
        self.assertNotIn(svc.snapshot()["run_id"], [r["run_id"] for r in runs],
                         "临时批次号应已从数据库删除")


class FavoritesSyncTests(unittest.TestCase):
    """收藏同步：一键将站点收藏全部标记为「待下载」。"""

    def setUp(self):
        self.tmp = make_temp_dir("favsync")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_want_download_for_favorites(self):
        db.upsert_items([item(1), item(2)], source_url="https://example.net/group/a/",
                        batch_id=1, path=self.db_path)
        # 模拟收藏同步：把收藏 ID 标记为待下载
        updated = db.set_want_download([1, 2], True, self.db_path)
        self.assertEqual(updated, 2)
        data = db.query_works(path=self.db_path, per_page=50)
        self.assertTrue(all(w["want_download"] for w in data["items"]))


class NewApiEndpointTests(unittest.TestCase):
    """新增 HTTP 接口的离线冒烟测试（不访问真实上游）。"""

    def setUp(self):
        self.tmp = make_temp_dir("newapi")
        self.db_path = self.tmp / "t.sqlite3"
        db.init_db(self.db_path)
        # 匿名（未登录）数据/站点设置落 unlogin_user 库（默认库不再存业务数据）
        anon_db = anon_db_path(self.db_path)
        db.init_db(anon_db)
        db.set_setting("site_origin", "https://example.net", anon_db)
        self.anon_db = anon_db
        self.server = ApiServer(("127.0.0.1", 0), Handler, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request_json(self, path, payload=None, method="GET"):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_hidden_endpoint_logically_deletes_and_filters(self):
        db.upsert_items([item(1), item(2)], source_url="https://example.net/group/a/",
                        batch_id=1, path=self.anon_db)
        status, result = self.request_json("/api/hidden", {"ids": [1], "hidden": True},
                                           method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(result["updated"], 1)
        _status, works = self.request_json("/api/works?per_page=50")
        self.assertEqual(works["total"], 1)
        self.assertEqual(works["items"][0]["gallery_id"], 2)

    def test_favorites_sync_marks_existing_new_items(self):
        db.upsert_items([item(1, "Existing")], source_url="https://example.net/group/a/",
                        batch_id=1, path=self.anon_db)

        class FakeUpstream:
            def __init__(self, *_args, **_kwargs):
                pass

            def list_favorites(self, page=1, query=""):
                if page > 1:
                    return {"items": [], "page": page, "num_pages": 1, "total": 0}
                return {
                    "items": [
                        {"gallery_id": 1, "title": "Remote Changed", "url": ""},
                        {"gallery_id": 2, "title": "Favorite New", "url": ""},
                    ],
                    "page": 1, "num_pages": 1, "total": 2,
                }

        original = server_module.UpstreamClient
        server_module.UpstreamClient = FakeUpstream
        self.addCleanup(setattr, server_module, "UpstreamClient", original)

        status, result = self.request_json("/api/favorites/sync?apikey=test-key", {}, method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(result["synced"], 2)
        _status, works = self.request_json("/api/works?per_page=50")
        by_id = {int(row["gallery_id"]): row for row in works["items"]}
        self.assertEqual(by_id[1]["title"], "Existing", "同步不应覆盖原始作品记录")
        self.assertTrue(by_id[1]["want_download"])
        self.assertTrue(by_id[1]["site_favorite"])
        self.assertTrue(by_id[2]["want_download"])
        self.assertTrue(by_id[2]["site_favorite"])


if __name__ == "__main__":
    unittest.main()
