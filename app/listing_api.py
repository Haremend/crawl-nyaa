"""列表抓取的 API 数据源：列表页 URL → /api/v2/search → PageResult(GalleryItem)。

站点对主域名的 HTML 列表页返回 403，抓取主链路改走公开搜索接口
（``/api/v2/search``，与收藏接口同构 payload）。本模块只负责「取一页并映射成
``GalleryItem``」：输出形状与 ``scraper.iter_pages`` 完全一致，因此入库/去重/
批次/增量/详情/封面等下游管线零改动；批次记录按规范化列表 URL 保存，历史批次
（/artist/、/group/、/search?q=… 等）继续可识别、增量匹配不变。

映射约定与 HTML 列表解析（``parser.parse_listing``）保持一致：
- 站内黑名单条目跳过（HTML 列表页本来就不显示它们）；
- 「uncensored」按普通标签进入 ``tags``（``uncensored`` 列仍由详情补全填）；
- 缩略图拼 ``t.<host>`` 子域（主域图片路径同样 403；与 ``detail._thumbnail_url`` 同规则）；
- ``title_key`` 用 ``parser.normalize_title_key``（列表标题指纹，去重兜底）；
- ``upload_date``/``num_pages`` 等详情字段列表接口没有，仍由详情补全填。
"""
from __future__ import annotations

from typing import Callable, Iterator
from urllib.parse import parse_qsl, urlsplit

from . import config
from .parser import TAG_KIND_FIELD, GalleryItem, normalize_title_key
from .scraper import FetchError, PageResult, _sleep, page_url
from .upstream import UpstreamClient, UpstreamError

# 与 scrape_service.LISTING_PREFIXES 对应的单段列表页前缀
_TAG_KINDS = ("artist", "group", "tag", "parody", "character", "language")


def listing_query_from_url(url: str) -> tuple[str, dict]:
    """列表页 URL → 搜索条件 ``(query, 附加参数)``。

    ``/artist/<slug>/`` → ``artist:"<slug>"``（group/tag/parody/character/language 同理）；
    ``/search?q=…`` → q 原样透传（含 ``title:"…"`` 这类短语与多词搜索）。
    两种形态的 ``sort`` 参数都透传。
    """
    parts = urlsplit(url)
    segments = [seg for seg in (parts.path or "/").strip("/").split("/") if seg]
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    extra = {"sort": str(params["sort"]).strip()} if params.get("sort") else {}
    if len(segments) >= 2 and segments[0] in _TAG_KINDS:
        return f'{segments[0]}:"{segments[1].lower()}"', extra
    if segments[:1] == ["search"]:
        query = str(params.get("q") or "").strip()
        if not query:
            raise FetchError("搜索页 URL 缺少 q 参数，无法抓取")
        return query, extra
    raise FetchError(
        "无法从该 URL 推导抓取条件（支持 /artist/、/group/、/tag/、/parody/、"
        f"/character/、/language/ 与 /search?q=…）：{url}")


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}"


def _thumb_url(origin: str, path: str) -> str:
    """缩略图 URL：API 返回相对路径，拼主域是 403，必须走 t.<host> 子域。"""
    path = (path or "").strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    parts = urlsplit(origin)
    scheme = parts.scheme or "https"
    host = parts.netloc
    if not host:
        return path
    if host.startswith("t."):
        return f"{scheme}://{host}/{path.lstrip('/')}"
    return f"{scheme}://t.{host}/{path.lstrip('/')}"


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def items_from_payload(payload: dict, *, origin: str, api: UpstreamClient) -> list[GalleryItem]:
    """搜索 payload → GalleryItem 列表。

    标签经 ``/tags/ids`` 批量解析后按类型归类（language → languages，
    artist/group/parody/character/category/tag → 各自字段）。
    """
    raw_items = [raw for raw in (payload.get("result") or [])
                 if isinstance(raw, dict) and not raw.get("blacklisted")]
    tag_ids: list[int] = []
    for raw in raw_items:
        for value in (raw.get("tag_ids") or []):
            tag_id = _int_or_none(value)
            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)
    tags = api.resolve_tags(tag_ids) if tag_ids else {}

    items: list[GalleryItem] = []
    for raw in raw_items:
        gallery_id = _int_or_none(raw.get("id")) or 0
        if gallery_id <= 0:
            continue
        item = GalleryItem(
            gallery_id=gallery_id,
            url=f"{origin}/g/{gallery_id}/",
            title=str(raw.get("english_title") or "").strip(),
            thumbnail=_thumb_url(origin, str(raw.get("thumbnail") or "")),
            media_id=_int_or_none(raw.get("media_id")),
        )
        for value in (raw.get("tag_ids") or []):
            tag = tags.get(_int_or_none(value) or 0)
            if tag is None:
                continue
            if tag.type == "language":
                if tag.slug and tag.slug not in item.languages:
                    item.languages.append(tag.slug)
                continue
            field = TAG_KIND_FIELD.get(tag.type)
            bucket = getattr(item, field, None) if field else None
            if bucket is None:
                continue
            text = (tag.name or "").strip() or (tag.slug or "").replace("-", " ")
            if text and text not in bucket:
                bucket.append(text)
        item.title_key = normalize_title_key(item.title) or f"id:{item.gallery_id}"
        items.append(item)
    return items


def iter_pages_api(
    url: str,
    *,
    api: UpstreamClient,
    max_pages: int = 0,
    delay: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> Iterator[PageResult]:
    """API 版翻页：产出与 ``scraper.iter_pages`` 同形状的 PageResult。

    ``page.url`` 用与 HTML 路径相同的 ``page_url()``（批次键仍是外部传入的
    规范化列表 URL，不受影响）；``last_page`` 直接取接口的 ``num_pages``。
    """
    query, extra = listing_query_from_url(url)
    origin = _origin_of(url)
    delay = config.PAGE_DELAY if delay is None else config.clamp_page_delay(delay)
    log = on_log or (lambda message: None)
    stopped = should_stop or (lambda: False)

    page_no = 1
    while True:
        if stopped():
            log("已取消抓取")
            return
        log(f"抓取第 {page_no} 页（搜索接口）")
        try:
            payload = api.search(query, page=page_no, **extra)
        except UpstreamError as exc:
            raise FetchError(str(exc)) from exc
        items = items_from_payload(payload, origin=origin, api=api)
        num_pages = max(1, _int_or_none(payload.get("num_pages")) or 1)
        log(f"第 {page_no} 页返回 {len(items)} 条作品（共 {num_pages} 页）")

        yield PageResult(
            page_no=page_no,
            url=page_url(url, page_no),
            items=items,
            last_page=num_pages,
            has_next=page_no < num_pages,
        )

        if max_pages and page_no >= max_pages:
            log(f"已达到页数上限（{max_pages} 页）")
            return
        if page_no >= num_pages:
            log("已是最后一页")
            return
        if not items:
            log("遇到空页，停止翻页")
            return
        page_no += 1
        _sleep(delay, stopped)
        if stopped():
            log("已取消抓取")
            return
