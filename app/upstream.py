"""上游 API 客户端（纯转发，不保存用户 API Key）。

设计约定：
- **公开接口不带 Key**：详情、标签查询、站点配置等，任何人都能调；
- **需要认证的接口才带 Key**：收藏列表、下载、账号信息；
- Key 只存在于单次请求的生命周期内，不落盘、不写日志、不入库。

站点地址（origin）不写死在代码里：
优先用环境变量，其次用数据库中最近一次抓取记录的地址。
"""
from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import urlencode

from . import config
from .scraper import FetchError, Fetcher


class UpstreamError(RuntimeError):
    """上游接口错误（含认证失败、限流等）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class UpstreamTag:
    id: int
    type: str
    name: str
    slug: str


@dataclass
class FavoriteItem:
    gallery_id: int
    media_id: int | None
    title: str
    title_original: str
    thumbnail: str
    num_pages: int | None = None
    num_favorites: int | None = None
    tag_ids: list[int] = field(default_factory=list)
    blacklisted: bool = False
    languages: list[str] = field(default_factory=list)
    uncensored: bool = False
    tags: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    parodies: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        """转成数据库行格式（供本地库复用同一套排序/展示逻辑）。"""
        return {
            "gallery_id": self.gallery_id,
            "media_id": self.media_id,
            "title": self.title or f"#{self.gallery_id}",
            "title_pretty": "",
            "title_original": self.title_original,
            "url": "",
            "thumbnail": self.thumbnail,
            "languages": list(self.languages),
            "tags": list(self.tags),
            "artists": list(self.artists),
            "groups": list(self.groups),
            "parodies": list(self.parodies),
            "characters": list(self.characters),
            "categories": list(self.categories),
            "num_pages": self.num_pages,
            "num_favorites": self.num_favorites,
            "uncensored": 1 if self.uncensored else 0,
            "site_favorite": 1,
        }


class UpstreamClient:
    """按需携带 API Key 的上游客户端。"""

    def __init__(self, site_origin: str, api_key: str = "", fetcher: Fetcher | None = None) -> None:
        self.site_origin = (site_origin or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.fetcher = fetcher or Fetcher()

    # ---------- 基础请求 ----------
    @property
    def api_base(self) -> str:
        return f"{self.site_origin}/api/v2"

    def _request(self, path: str, *, method: str = "GET", auth: bool = False,
                 params: dict | None = None, body: dict | None = None) -> object:
        if not self.site_origin:
            raise UpstreamError("未配置站点地址：先抓取一次列表页，或设置环境变量 CLAW_API_ORIGIN", 500)
        if auth and not self.api_key:
            raise UpstreamError("该接口需要 API Key", 401)

        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.api_base}{path}{query}"
        headers = {"Accept": "application/json"}
        # 只有需要认证的接口才带上 Key
        if auth:
            headers["Authorization"] = f"Key {self.api_key}"
        data = json.dumps(body).encode("utf-8") if body is not None else (b"" if method == "POST" else None)
        if body is not None:
            headers["Content-Type"] = "application/json"

        try:
            raw, _ct = self.fetcher.request(url, method=method, headers=headers, data=data,
                                            referer=f"{self.site_origin}/")
        except FetchError as exc:
            message = str(exc)
            status = None
            if "HTTP 401" in message:
                status = 401
            elif "HTTP 403" in message:
                status = 403
            elif "HTTP 429" in message:
                status = 429
            raise UpstreamError(message, status) from exc
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise UpstreamError("上游返回的不是 JSON", 502) from exc

    # ---------- 公开接口（不需要 Key） ----------
    def get_gallery(self, gallery_id: int, include: str = "") -> dict:
        params = {"include": include} if include else None
        data = self._request(f"/galleries/{int(gallery_id)}", auth=False, params=params)
        return data if isinstance(data, dict) else {}

    def resolve_tags(self, tag_ids: list[int]) -> dict[int, UpstreamTag]:
        """批量解析标签 id（公开接口，单次最多 100 个）。"""
        ids = [int(i) for i in tag_ids if i]
        if not ids:
            return {}
        result: dict[int, UpstreamTag] = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            data = self._request("/tags/ids", auth=False, params={"ids": ",".join(str(i) for i in chunk)})
            items = data if isinstance(data, list) else (data.get("result") if isinstance(data, dict) else [])
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                try:
                    tag_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                result[tag_id] = UpstreamTag(
                    id=tag_id,
                    type=str(item.get("type") or ""),
                    name=str(item.get("name") or ""),
                    slug=str(item.get("slug") or ""),
                )
        return result

    # ---------- 需要 Key 的接口 ----------
    def get_user(self) -> dict:
        data = self._request("/user", auth=True)
        return data if isinstance(data, dict) else {}

    def get_favorites(self, page: int = 1, query: str = "") -> dict:
        params: dict = {"page": max(1, int(page))}
        if query:
            params["q"] = query
        data = self._request("/favorites", auth=True, params=params)
        return data if isinstance(data, dict) else {}

    def get_download_url(self, gallery_id: int, fmt: str = "zip") -> dict:
        fmt = fmt if fmt in ("zip", "cbz", "torrent") else "zip"
        data = self._request(f"/galleries/{int(gallery_id)}/download", method="POST",
                             auth=True, params={"format": fmt})
        return data if isinstance(data, dict) else {}

    # ---------- 复合：收藏列表 + 标签解析 ----------
    def list_favorites(self, page: int = 1, query: str = "") -> dict:
        """返回 {items: [FavoriteItem], total, per_page, num_pages, page}。"""
        payload = self.get_favorites(page=page, query=query)
        raw_items = payload.get("result") or []
        items: list[FavoriteItem] = []

        tag_ids: list[int] = []
        for raw in raw_items:
            for value in (raw.get("tag_ids") or []):
                try:
                    tag_id = int(value)
                except (TypeError, ValueError):
                    continue
                if tag_id not in tag_ids:
                    tag_ids.append(tag_id)

        # 标签解析走公开接口（不带 Key）
        tags = self.resolve_tags(tag_ids)

        for raw in raw_items:
            thumbnail_path = str(raw.get("thumbnail") or "").lstrip("/")
            item = FavoriteItem(
                gallery_id=int(raw.get("id") or 0),
                media_id=_int_or_none(raw.get("media_id")),
                title=str(raw.get("english_title") or ""),
                title_original=str(raw.get("japanese_title") or ""),
                thumbnail=f"{self.site_origin}/{thumbnail_path}" if thumbnail_path else "",
                num_pages=_int_or_none(raw.get("num_pages")),
                num_favorites=_int_or_none(raw.get("num_favorites")),
                tag_ids=list(raw.get("tag_ids") or []),
                blacklisted=bool(raw.get("blacklisted")),
            )
            for tag_id in item.tag_ids:
                tag = tags.get(int(tag_id))
                if tag is None:
                    continue
                if tag.type == "language":
                    if tag.slug and tag.slug not in item.languages:
                        item.languages.append(tag.slug)
                    continue
                if tag.slug == "uncensored":
                    item.uncensored = True
                bucket = {
                    "artist": item.artists, "group": item.groups, "parody": item.parodies,
                    "character": item.characters, "category": item.categories, "tag": item.tags,
                }.get(tag.type)
                if bucket is not None and tag.name and tag.name not in bucket:
                    bucket.append(tag.name)
            items.append(item)

        return {
            "items": [item.as_row() for item in items],
            "page": int(payload.get("page") or page),
            "num_pages": int(payload.get("num_pages") or 1),
            "per_page": int(payload.get("per_page") or len(items) or 25),
            "total": int(payload.get("total") or len(items)),
        }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None