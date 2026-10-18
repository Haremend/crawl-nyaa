"""HTTP 抓取：走本地 HTTP 代理、失败重试、限速翻页。"""
from __future__ import annotations

import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config
from .parser import GalleryItem, parse_listing


class FetchError(RuntimeError):
    """网络请求失败（代理不可用、超时、HTTP 错误等）。"""


@dataclass
class PageResult:
    page_no: int
    url: str
    items: list[GalleryItem]
    last_page: int | None
    has_next: bool
    used_fallback: bool = False


def page_url(base_url: str, page_no: int) -> str:
    """基于列表页 URL 生成第 N 页 URL（去掉原有的 page 参数）。"""
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
    if page_no > 1:
        query.append(("page", str(page_no)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _retry_after(exc) -> float | None:
    try:
        value = exc.headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(0.5, min(30.0, float(value)))
    except (TypeError, ValueError):
        return None


class Fetcher:
    """基于 urllib 的抓取器；默认使用本地 HTTP 代理 http://127.0.0.1:10808。"""

    def __init__(
        self,
        proxy: str | None = None,
        *,
        timeout: float | None = None,
        user_agent: str | None = None,
        retries: int | None = None,
    ) -> None:
        # 默认跟随运行时的「开启代理」开关；显式传入 proxy 时以传入值为准
        self.proxy = config.effective_proxy() if proxy is None else proxy
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.user_agent = user_agent or config.USER_AGENT
        self.retries = max(1, retries or config.REQUEST_RETRIES)
        handlers: list = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        if config.INSECURE_TLS:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def _headers(self, referer: str | None = None) -> dict:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _hint(self, message: str) -> str:
        if self.proxy:
            return f"{message}，请确认本地代理 {self.proxy} 正在运行"
        return message

    def request(self, url: str, *, method: str = "GET", headers: dict | None = None,
                data: bytes | None = None, referer: str | None = None) -> tuple[bytes, str]:
        """通用请求（可自定义方法/头部/请求体），复用同一套代理与重试逻辑。"""
        merged = self._headers(referer)
        merged.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=merged, method=method)
        body, content_type, _charset = self._open_with_retry(req, url)
        return body, content_type

    def _open_with_retry(self, request: urllib.request.Request, url: str) -> tuple[bytes, str, str]:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            wait = 1.5 * attempt + random.uniform(0, 0.4)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                    content_type = response.headers.get_content_type() or "application/octet-stream"
                    charset = response.headers.get_content_charset() or "utf-8"
                    return body, content_type, charset
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code in (408, 429, 500, 502, 503, 504)
                if not retryable or attempt >= self.retries:
                    raise FetchError(self._hint(f"HTTP {exc.code} {exc.reason}（{url}）")) from exc
                wait = _retry_after(exc) or wait
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    reason = getattr(exc, "reason", None) or exc
                    raise FetchError(self._hint(f"网络错误：{reason}（{url}）")) from exc
            time.sleep(wait)
        raise FetchError(self._hint(f"请求失败：{last_error}（{url}）"))

    def fetch_raw(self, url: str, *, referer: str | None = None) -> tuple[bytes, str, str]:
        """GET 请求，返回 (响应体, content_type, charset)。"""
        request = urllib.request.Request(url, headers=self._headers(referer))
        return self._open_with_retry(request, url)

    def fetch_text(self, url: str, *, referer: str | None = None) -> str:
        body, _content_type, charset = self.fetch_raw(url, referer=referer)
        try:
            return body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    def fetch_bytes(self, url: str, *, referer: str | None = None) -> tuple[bytes, str]:
        body, content_type, _charset = self.fetch_raw(url, referer=referer)
        return body, content_type


def _sleep(seconds: float, stopped: Callable[[], bool]) -> None:
    remaining = max(0.0, seconds) + random.uniform(0, 0.35)
    while remaining > 0:
        if stopped():
            return
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step


def iter_pages(
    url: str,
    *,
    max_pages: int = 0,
    delay: float | None = None,
    fetcher: Fetcher | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> Iterator[PageResult]:
    """逐页抓取列表并产出 PageResult；调用方负责入库与统计。"""
    fetcher = fetcher or Fetcher()
    delay = config.PAGE_DELAY if delay is None else max(config.MIN_PAGE_DELAY, delay)
    log = on_log or (lambda message: None)
    stopped = should_stop or (lambda: False)

    page_no = 1
    last_page: int | None = None
    while True:
        if stopped():
            log("已取消抓取")
            return
        current = page_url(url, page_no)
        log(f"抓取第 {page_no} 页：{current}")
        html = fetcher.fetch_text(current, referer=url)
        page = parse_listing(html, current)
        if page.used_fallback:
            log("提示：使用兼容解析模式（页面结构与预期不完全一致）")
        last_page = page.last_page or last_page
        log(f"第 {page_no} 页解析到 {len(page.items)} 条作品")

        yield PageResult(
            page_no=page_no,
            url=current,
            items=page.items,
            last_page=last_page,
            has_next=page.has_next,
            used_fallback=page.used_fallback,
        )

        if max_pages and page_no >= max_pages:
            log(f"已达到页数上限（{max_pages} 页）")
            return
        if last_page and page_no >= last_page:
            return
        if not page.items:
            log("遇到空页，停止翻页")
            return
        if not page.has_next and not last_page and len(page.items) < config.PAGE_SIZE_HINT:
            log("已是最后一页")
            return
        page_no += 1
        _sleep(delay, stopped)
        if stopped():
            log("已取消抓取")
            return