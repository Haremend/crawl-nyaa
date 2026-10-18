"""封面下载：把作品封面保存到项目 cover/ 目录。

- 只允许 http/https 图片地址；
- 文件名固定为 <gallery_id>.<ext>，便于前端和 SQLite 关联；
- 已存在同名文件时跳过（幂等），失败不写库，下次抓取自动重试。
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .scraper import FetchError, Fetcher

ALLOWED_EXTENSIONS = {"webp", "jpg", "jpeg", "png", "gif", "avif"}
CONTENT_TYPE_EXT = {
    "image/webp": "webp",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/avif": "avif",
}
SAFE_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")


def is_media_url(url: str) -> bool:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    # 只允许 http/https 图片地址（域名由用户输入的列表页决定）
    return bool(parts.scheme in ('http', 'https') and parts.netloc)


def extension_for(url: str, content_type: str = "") -> str:
    suffix = Path(urlsplit(url or "").path).suffix.lower().lstrip(".")
    if suffix in ALLOWED_EXTENSIONS:
        return "jpg" if suffix == "jpeg" else suffix
    kind = (content_type or "").split(";")[0].strip().lower()
    return CONTENT_TYPE_EXT.get(kind, "webp")


def cover_filename(gallery_id: int, extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        ext = "webp"
    return f"{int(gallery_id)}.{'jpg' if ext == 'jpeg' else ext}"


def is_safe_filename(name: str) -> bool:
    return bool(name) and SAFE_NAME_RE.match(name) is not None


def download_cover(
    gallery_id: int,
    url: str,
    *,
    cover_dir: Path,
    fetcher: Fetcher | None = None,
    site_domain: str = "",
    max_bytes: int = 12 * 1024 * 1024,
) -> str | None:
    """下载单个封面，返回保存的文件名；已存在或失败时返回 None/已有文件名。"""
    if not gallery_id or not is_media_url(url):
        return None
    cover_dir = Path(cover_dir)
    cover_dir.mkdir(parents=True, exist_ok=True)

    # 已存在（任意扩展名）直接复用，避免重复请求
    for existing in cover_dir.glob(f"{int(gallery_id)}.*"):
        if existing.is_file() and existing.stat().st_size > 0:
            return existing.name

    data_fetcher = fetcher or Fetcher()
    try:
        referer = f"https://{site_domain}/g/{int(gallery_id)}/" if site_domain else None
        body, content_type = data_fetcher.fetch_bytes(url, referer=referer)
    except FetchError:
        return None
    if not body or len(body) > max_bytes:
        return None
    filename = cover_filename(gallery_id, extension_for(url, content_type))
    target = cover_dir / filename
    try:
        target.write_bytes(body)
    except OSError:
        return None
    return filename