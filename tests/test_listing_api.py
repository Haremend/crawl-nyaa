"""列表抓取 API 数据源的单测：URL→查询映射、payload→GalleryItem 映射。

对应 app/listing_api.py（列表页 HTML 被站点 403 后，抓取改走 /api/v2/search）。
全部离线：resolve_tags 用内存桩，不发网络请求。
"""
import unittest

from app import config
from app.listing_api import _thumb_url, items_from_payload, listing_query_from_url
from app.scraper import FetchError
from app.upstream import UpstreamTag


class FakeTags:
    """resolve_tags 桩：只有 search 方法会被用到。"""

    def __init__(self, tags):
        self.tags = tags            # tag_id -> UpstreamTag
        self.calls = []

    def resolve_tags(self, tag_ids):
        self.calls.append(list(tag_ids))
        return {tid: self.tags[tid] for tid in tag_ids if tid in self.tags}


def tag(tag_id, kind, name, slug=""):
    return UpstreamTag(id=tag_id, type=kind, name=name, slug=slug or name.lower())


class ListingQueryTests(unittest.TestCase):
    """批次历史里的各种列表 URL 形态都要能映射成搜索条件。"""

    def test_tag_style_urls_map_to_quoted_queries(self):
        cases = {
            "https://example.net/artist/sample-artist/": 'artist:"sample-artist"',
            "https://example.net/group/samplecircle/": 'group:"samplecircle"',
            "https://example.net/tag/sample-tag/": 'tag:"sample-tag"',
            "https://example.net/parody/sample-series/": 'parody:"sample-series"',
            "https://example.net/character/sample-chan/": 'character:"sample-chan"',
            "https://example.net/language/chinese/": 'language:"chinese"',
        }
        for url, expected in cases.items():
            query, extra = listing_query_from_url(url)
            self.assertEqual(query, expected, url)
            self.assertEqual(extra, {})

    def test_search_url_passes_query_through(self):
        # 历史批次里的真实形态：带引号短语、空格多词、sort 参数
        query, extra = listing_query_from_url(
            "https://example.net/search?q=title%3A%22Sample+Name%22")
        self.assertEqual(query, 'title:"Sample Name"')
        self.assertEqual(extra, {})
        query, extra = listing_query_from_url(
            "https://example.net/search?q=SOME+MAG+Decensored&sort=popular")
        self.assertEqual(query, "SOME MAG Decensored")
        self.assertEqual(extra, {"sort": "popular"})

    def test_sort_is_forwarded_for_tag_style_urls(self):
        query, extra = listing_query_from_url(
            "https://example.net/artist/sample-artist/?sort=popular")
        self.assertEqual(query, 'artist:"sample-artist"')
        self.assertEqual(extra, {"sort": "popular"})

    def test_unmappable_url_raises(self):
        with self.assertRaises(FetchError):
            listing_query_from_url("https://example.net/g/123/")
        with self.assertRaises(FetchError):
            listing_query_from_url("https://example.net/search")


class ThumbUrlTests(unittest.TestCase):
    def test_thumbnail_uses_t_subdomain(self):
        """主域图片路径 403，必须拼 t.<host>（与 detail._thumbnail_url 同规则）。"""
        self.assertEqual(
            _thumb_url("https://example.net", "galleries/123/thumb.jpg.webp"),
            "https://t.example.net/galleries/123/thumb.jpg.webp")
        self.assertEqual(_thumb_url("https://example.net", "/galleries/1/t.jpg"),
                         "https://t.example.net/galleries/1/t.jpg")
        # 已是绝对地址/已是 t. 子域时原样保留
        self.assertEqual(_thumb_url("https://example.net", "https://cdn.example.net/x.jpg"),
                         "https://cdn.example.net/x.jpg")
        self.assertEqual(_thumb_url("https://t.example.net", "galleries/1/t.jpg"),
                         "https://t.example.net/galleries/1/t.jpg")
        self.assertEqual(_thumb_url("https://example.net", ""), "")


class ItemsFromPayloadTests(unittest.TestCase):
    def _payload(self, items):
        return {"result": items, "page": 1, "num_pages": 1, "per_page": 25,
                "total": len(items)}

    def test_maps_fields_and_tag_buckets(self):
        tags = {
            10: tag(10, "language", "Chinese", "chinese"),
            11: tag(11, "artist", "Sample Artist"),
            12: tag(12, "group", "SampleCircle"),
            13: tag(13, "parody", "Sample Series"),
            14: tag(14, "character", "Sample Chan"),
            15: tag(15, "category", "Doujinshi"),
            16: tag(16, "tag", "Uncensored"),
        }
        api = FakeTags(tags)
        payload = self._payload([{
            "id": 9001, "media_id": 555, "english_title": "Sample Work [English]",
            "japanese_title": "サンプル", "thumbnail": "galleries/555/thumb.jpg.webp",
            "tag_ids": [10, 11, 12, 13, 14, 15, 16], "blacklisted": False,
        }])
        items = items_from_payload(payload, origin="https://example.net", api=api)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.gallery_id, 9001)
        self.assertEqual(item.media_id, 555)
        self.assertEqual(item.title, "Sample Work [English]")
        self.assertEqual(item.url, "https://example.net/g/9001/")
        self.assertEqual(item.thumbnail, "https://t.example.net/galleries/555/thumb.jpg.webp")
        self.assertEqual(item.languages, ["chinese"])
        self.assertEqual(item.artists, ["Sample Artist"])
        self.assertEqual(item.groups, ["SampleCircle"])
        self.assertEqual(item.parodies, ["Sample Series"])
        self.assertEqual(item.characters, ["Sample Chan"])
        self.assertEqual(item.categories, ["Doujinshi"])
        self.assertEqual(item.tags, ["Uncensored"], "uncensored 按普通标签进 tags（与 HTML 列表一致）")
        self.assertTrue(item.title_key, "title_key 必须计算（去重兜底）")

    def test_blacklisted_items_are_skipped(self):
        api = FakeTags({})
        payload = self._payload([
            {"id": 1, "english_title": "A", "tag_ids": [], "blacklisted": False},
            {"id": 2, "english_title": "B", "tag_ids": [], "blacklisted": True},
        ])
        items = items_from_payload(payload, origin="https://example.net", api=api)
        self.assertEqual([i.gallery_id for i in items], [1])

    def test_invalid_rows_are_skipped_and_keys_resolved_once(self):
        api = FakeTags({20: tag(20, "tag", "X")})
        payload = self._payload([
            {"id": 0, "english_title": "坏行", "tag_ids": [20]},
            {"id": 7, "english_title": "好行", "tag_ids": [20, 20]},
        ])
        items = items_from_payload(payload, origin="https://example.net", api=api)
        self.assertEqual([i.gallery_id for i in items], [7])
        self.assertEqual(api.calls, [[20]], "tag_id 去重后只解析一次")

    def test_title_key_matches_parser_convention(self):
        """同一标题在 HTML 路径与 API 路径下指纹必须一致（跨来源去重）。"""
        from app.parser import normalize_title_key
        api = FakeTags({})
        payload = self._payload([{"id": 42, "english_title": "Same Title [Digital]",
                                  "tag_ids": []}])
        item = items_from_payload(payload, origin="https://example.net", api=api)[0]
        self.assertEqual(item.title_key, normalize_title_key("Same Title [Digital]"))


if __name__ == "__main__":
    unittest.main()
