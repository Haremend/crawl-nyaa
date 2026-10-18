"""画廊列表页解析器（仅使用标准库，不依赖 BeautifulSoup）。

列表页结构（经典版页面）：
    <div class="gallery">
      <a class="cover" href="/g/<id>/"><img data-src="https://t3.example.net/galleries/<media>/1t.jpg"></a>
      <div class="caption">标题</div>
      <div class="tag-container">
        <a class="tag" href="/language/english/"><span class="name">english</span></a>
        ...
      </div>
    </div>
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from . import config
from .titles import title_meta

GALLERY_HREF_RE = re.compile(r"^/g/(\d+)/?$")
MEDIA_ID_RE = re.compile(r"/galleries/(\d+)/")
PAGE_PARAM_RE = re.compile(r"[?&]page=(\d+)")
TAG_HREF_RE = re.compile(r"^/(language|tag|artist|group|parody|character|category)/([^/?#]+)/?$")

TAG_KIND_FIELD = {
    "language": "languages",
    "artist": "artists",
    "group": "groups",
    "parody": "parodies",
    "character": "characters",
    "category": "categories",
    "tag": "tags",
}


@dataclass
class GalleryItem:
    gallery_id: int
    url: str
    title: str = ""
    thumbnail: str = ""
    media_id: int | None = None
    languages: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    parodies: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    title_key: str = ""

    def as_row(self) -> dict:
        return {
            "gallery_id": self.gallery_id,
            "media_id": self.media_id,
            "title": self.title,
            "title_key": self.title_key,
            "url": self.url,
            "thumbnail": self.thumbnail,
            "languages": list(self.languages),
            "tags": list(self.tags),
            "artists": list(self.artists),
            "groups": list(self.groups),
            "parodies": list(self.parodies),
            "characters": list(self.characters),
            "categories": list(self.categories),
        }


@dataclass
class ListingPage:
    items: list[GalleryItem]
    last_page: int | None = None
    has_next: bool = False
    current_page: int = 1
    used_fallback: bool = False


def clean_text(text: str) -> str:
    text = (text or "").replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_title_key(title: str) -> str:
    """标题归一化（同作品去重的兜底 key）。

    统一使用 titles 模块的指纹规则：先剥离 [English]/[Chinese]/[Digital]/[汉化组]
    这类噪声括号，再对剩余核心文本做 NFKC + 小写 + 去标点处理。
    详情补全拿到的原始标题会用同一套规则生成 original_key 做精确分组。
    """
    return title_meta(title)["core"]


def classify_tag(href: str) -> tuple[str, str] | None:
    """把标签链接解析成 (类型, slug)，例如 /language/english/ -> ("language", "english")。"""
    try:
        path = urlsplit(href or "").path
    except ValueError:
        return None
    match = TAG_HREF_RE.match(path)
    if not match:
        return None
    return match.group(1), match.group(2).lower()


def _media_id(thumbnail: str) -> int | None:
    match = MEDIA_ID_RE.search(thumbnail or "")
    return int(match.group(1)) if match else None


class _ListingParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: list[GalleryItem] = []
        self.last_anchor_page: int | None = None
        self.max_page_seen = 0
        self.next_found = False

        self._item: GalleryItem | None = None
        self._item_depth = 0
        self._caption_depth = 0
        self._caption_text: list[str] = []
        self._tag_ctx: dict | None = None
        self._in_cover = False
        self._cover_thumb = ""
        self._alt_text = ""

    # ---------- 解析回调 ----------
    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.lower()
        attr = {key.lower(): (value or "") for key, value in attrs}
        classes = (attr.get("class") or "").split()

        if name == "div":
            if self._item is None:
                if "gallery" in classes:
                    self._item = GalleryItem(gallery_id=0, url="")
                    self._item_depth = 1
                    self._cover_thumb = ""
                    self._caption_depth = 0
                    self._caption_text = []
                    self._alt_text = ""
                    # 新版列表页用 lang-xx class 标记语言（如 lang-gb / lang-jp / lang-cn）
                    for css_class in classes:
                        if css_class.startswith("lang-"):
                            code = css_class[5:].strip().lower()
                            if code:
                                language = config.LANGUAGE_CLASS_MAP.get(code, code)
                                if language not in self._item.languages:
                                    self._item.languages.append(language)
            else:
                self._item_depth += 1
                if self._caption_depth == 0 and "caption" in classes:
                    self._caption_depth = 1
                    self._caption_text = []
                elif self._caption_depth > 0:
                    self._caption_depth += 1
            return

        if name == "a":
            href = attr.get("href") or ""
            page_match = PAGE_PARAM_RE.search(href)
            if page_match:
                page_no = int(page_match.group(1))
                self.max_page_seen = max(self.max_page_seen, page_no)
                if "last" in classes:
                    self.last_anchor_page = max(self.last_anchor_page or 0, page_no)
                if "next" in classes:
                    self.next_found = True
            if self._item is None:
                return
            if GALLERY_HREF_RE.match(urlsplit(href).path):
                if not self._item.url:
                    self._item.url = urljoin(self.base_url, href)
                    match = GALLERY_HREF_RE.match(urlsplit(self._item.url).path)
                    if match:
                        self._item.gallery_id = int(match.group(1))
            if "cover" in classes:
                self._in_cover = True
            if "tag" in classes:
                self._tag_ctx = {"href": href, "text": []}
            return

        if name == "img" and self._item is not None:
            alt = clean_text(attr.get("alt") or "")
            if alt and not self._alt_text:
                self._alt_text = alt
            src = (attr.get("data-src") or attr.get("data-lazy-src")
                   or attr.get("data-original") or attr.get("src") or "").strip()
            if not src or src.startswith("data:"):
                return
            absolute = urljoin(self.base_url, src)
            if self._in_cover:
                if not self._cover_thumb:
                    self._cover_thumb = absolute
                    self._item.thumbnail = absolute
            elif not self._item.thumbnail:
                self._item.thumbnail = absolute

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "a":
            if self._tag_ctx is not None:
                self._finish_tag()
            self._in_cover = False
            return
        if name == "div" and self._item is not None:
            if self._caption_depth > 0:
                self._caption_depth -= 1
                if self._caption_depth == 0:
                    title = clean_text("".join(self._caption_text))
                    if title and not self._item.title:
                        self._item.title = title
                    self._caption_text = []
            self._item_depth -= 1
            if self._item_depth <= 0:
                self._finish_item()

    def handle_data(self, data: str) -> None:
        if self._tag_ctx is not None:
            self._tag_ctx["text"].append(data)
        elif self._item is not None and self._caption_depth > 0:
            self._caption_text.append(data)

    # ---------- 内部工具 ----------
    def _finish_tag(self) -> None:
        ctx, self._tag_ctx = self._tag_ctx, None
        if not ctx or self._item is None:
            return
        classified = classify_tag(ctx.get("href", ""))
        if not classified:
            return
        kind, slug = classified
        if kind == "language":
            if slug not in self._item.languages:
                self._item.languages.append(slug)
            return
        text = clean_text("".join(ctx.get("text", []))) or slug.replace("-", " ")
        bucket: list[str] = getattr(self._item, TAG_KIND_FIELD[kind])
        if text and text not in bucket:
            bucket.append(text)

    def _finish_item(self) -> None:
        item, self._item = self._item, None
        self._item_depth = 0
        self._caption_depth = 0
        self._caption_text = []
        self._tag_ctx = None
        self._in_cover = False
        self._cover_thumb = ""
        alt_text, self._alt_text = self._alt_text, ""
        if item is None or item.gallery_id <= 0:
            return
        if not item.url:
            item.url = f"/g/{item.gallery_id}/"
        item.title = clean_text(item.title) or alt_text or f"#{item.gallery_id}"
        item.title_key = normalize_title_key(item.title) or f"id:{item.gallery_id}"
        item.media_id = _media_id(item.thumbnail)
        self.items.append(item)


def _fallback_parse(html: str, base_url: str) -> list[GalleryItem]:
    """页面结构变化时的兼容解析（基于正则的降级方案）。"""
    items: list[GalleryItem] = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bgallery\b[^"]*"', html or "", flags=re.IGNORECASE)[1:]
    if not blocks:
        blocks = [html or ""]
    for block in blocks:
        href_match = re.search(r'href="(/g/(\d+)/?)"', block)
        if not href_match:
            continue
        item = GalleryItem(
            gallery_id=int(href_match.group(2)),
            url=urljoin(base_url, href_match.group(1)),
        )
        title_match = re.search(r'class="[^"]*\bcaption\b[^"]*"[^>]*>(.*?)</div>', block, re.S | re.I)
        if title_match:
            item.title = clean_text(re.sub(r"<[^>]+>", " ", title_match.group(1)))
        thumb_match = re.search(r'\bdata-src="([^"]+)"', block) or re.search(r'<img[^>]+\bsrc="([^"]+)"', block)
        if thumb_match and not thumb_match.group(1).startswith("data:"):
            item.thumbnail = urljoin(base_url, thumb_match.group(1))
        for tag_match in re.finditer(
            r'<a[^>]+class="[^"]*\btag\b[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.S | re.I,
        ):
            classified = classify_tag(tag_match.group(1))
            if not classified:
                continue
            kind, slug = classified
            if kind == "language":
                if slug not in item.languages:
                    item.languages.append(slug)
                continue
            text = clean_text(re.sub(r"<[^>]+>", " ", tag_match.group(2))) or slug.replace("-", " ")
            bucket: list[str] = getattr(item, TAG_KIND_FIELD[kind])
            if text and text not in bucket:
                bucket.append(text)
        item.title = clean_text(item.title) or f"#{item.gallery_id}"
        item.title_key = normalize_title_key(item.title) or f"id:{item.gallery_id}"
        item.media_id = _media_id(item.thumbnail)
        items.append(item)
    return items


def parse_listing(html: str, base_url: str) -> ListingPage:
    """解析一页列表 HTML，返回 ListingPage。"""
    parser = _ListingParser(base_url)
    parser.feed(html or "")
    parser.close()
    if parser._item is not None:  # 文档结束时仍未闭合的条目
        parser._finish_item()

    items = parser.items
    used_fallback = False
    if not items:
        items = _fallback_parse(html, base_url)
        used_fallback = bool(items)

    unique: dict[int, GalleryItem] = {}
    for item in items:
        unique.setdefault(item.gallery_id, item)

    try:
        current_page = int(parse_qs(urlsplit(base_url).query).get("page", ["1"])[0])
    except (TypeError, ValueError):
        current_page = 1

    # 只有 "Last" 链接给出的页码才当作准确末页；
    # 编号分页窗口（1..5）不能当作总页数，避免提前中断翻页。
    last_page = parser.last_anchor_page
    if last_page is None and not parser.next_found and parser.max_page_seen > current_page:
        last_page = parser.max_page_seen
    has_next = parser.next_found or (last_page is not None and current_page < last_page)

    return ListingPage(
        items=list(unique.values()),
        last_page=last_page,
        has_next=has_next,
        current_page=current_page,
        used_fallback=used_fallback,
    )