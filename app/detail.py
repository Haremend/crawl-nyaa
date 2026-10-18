"""作品详情补全：调用 站内 JSON API 获取原始标题与完整标签。

接口：GET https://example.net/api/v2/galleries/{id}
返回：title.{english,japanese,pretty}、tags（language/artist/group/parody/...）、
      num_pages、num_favorites、upload_date、scanlator 等。

原始标题（title.japanese）去掉 [英訳]/[中国翻訳]/[DL版] 这类语言格式括号后，
同一作品的所有语言版本完全一致，可作为精确去重键。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .scraper import FetchError, Fetcher
from .titles import title_meta

def detail_api_url(site_domain: str, gallery_id: int) -> str:
    """根据列表页域名拼接详情 API 地址。"""
    return f"https://{site_domain}/api/v2/galleries/{gallery_id}"

# 这些"语言"标签是标记而不是语言，过滤时直接忽略
LANGUAGE_PSEUDO_TAGS = {"translated", "speechless", "text cleaned", "rewrite"}

TAG_BUCKETS = {
    "artist": "artists",
    "group": "groups",
    "parody": "parodies",
    "character": "characters",
    "category": "categories",
    "tag": "tags",
}


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _thumbnail_url(payload: dict, site_domain: str = "") -> str:
    """详情 API 的 thumbnail 可能是对象（{'path': 'galleries/<media>/thumb.webp'}），
    也可能是字符串。统一转成可直接请求的完整 URL。"""
    raw = payload.get("thumbnail")
    path = ""
    if isinstance(raw, dict):
        path = _text(raw.get("path"))
    elif isinstance(raw, str):
        path = raw.strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    domain = site_domain or "localhost"
    return f"https://t.{domain}/" + path.lstrip("/")


def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _iso_from_epoch(value) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class GalleryDetail:
    gallery_id: int
    media_id: int | None = None
    title_english: str = ""
    title_japanese: str = ""
    title_pretty: str = ""
    thumbnail: str = ""
    scanlator: str = ""
    num_pages: int | None = None
    num_favorites: int | None = None
    upload_date: str = ""
    uncensored: bool = False
    languages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    parodies: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @property
    def title_original(self) -> str:
        return self.title_japanese or self.title_english

    @property
    def original_key(self) -> str:
        if not self.title_original:
            return ""
        return title_meta(self.title_original)["core"]

    def as_row(self) -> dict:
        return {
            "gallery_id": self.gallery_id,
            "media_id": self.media_id,
            "title_original": self.title_original,
            "original_key": self.original_key,
            "title_pretty": self.title_pretty,
            "thumbnail": self.thumbnail,
            "scanlator": self.scanlator,
            "num_pages": self.num_pages,
            "num_favorites": self.num_favorites,
            "upload_date": self.upload_date,
            "languages": list(self.languages),
            "tags": list(self.tags),
            "artists": list(self.artists),
            "groups": list(self.groups),
            "parodies": list(self.parodies),
            "characters": list(self.characters),
            "categories": list(self.categories),
            "uncensored": bool(self.uncensored),
        }


def parse_detail_payload(payload: dict, site_domain: str = "") -> GalleryDetail:
    """把 /api/v2/galleries/{id} 的 JSON 解析成 GalleryDetail。"""
    if not isinstance(payload, dict):
        raise ValueError("详情数据不是 JSON 对象")
    title = payload.get("title") if isinstance(payload.get("title"), dict) else {}
    detail = GalleryDetail(
        gallery_id=int(payload.get("id") or 0),
        media_id=_int_or_none(payload.get("media_id")),
        title_english=_text(title.get("english")),
        title_japanese=_text(title.get("japanese")),
        title_pretty=_text(title.get("pretty")),
        thumbnail=_thumbnail_url(payload, site_domain),
        scanlator=_text(payload.get("scanlator")),
        num_pages=_int_or_none(payload.get("num_pages")),
        num_favorites=_int_or_none(payload.get("num_favorites")),
        upload_date=_iso_from_epoch(payload.get("upload_date")),
        uncensored=any(
            tag.get("type") == "tag" and str(tag.get("name") or "").strip().lower() == "uncensored"
            for tag in (payload.get("tags") or []) if isinstance(tag, dict)
        ),
    )
    for tag in payload.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        kind = _text(tag.get("type")).lower()
        name = _text(tag.get("name"))
        slug = (_text(tag.get("slug")) or name).lower()
        if not name:
            continue
        if kind == "language":
            if slug and slug not in LANGUAGE_PSEUDO_TAGS and slug not in detail.languages:
                detail.languages.append(slug)
            continue
        bucket_name = TAG_BUCKETS.get(kind)
        if bucket_name:
            bucket = getattr(detail, bucket_name)
            if name not in bucket:
                bucket.append(name)
    return detail


def fetch_detail(gallery_id: int, fetcher: Fetcher | None = None, site_domain: str = "") -> GalleryDetail | None:
    """抓取单个作品详情；失败返回 None（由调用方记录并跳过）。"""
    data_fetcher = fetcher or Fetcher()
    url = detail_api_url(site_domain, gallery_id)
    try:
        body, _content_type = data_fetcher.fetch_bytes(url, referer=f"https://{site_domain}/g/{int(gallery_id)}/" if site_domain else None)
    except FetchError:
        return None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    try:
        return parse_detail_payload(payload, site_domain)
    except ValueError:
        return None