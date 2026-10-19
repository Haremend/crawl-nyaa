"""claw-gallery 全局配置。

所有配置都可以通过环境变量覆盖，默认值面向本机使用：
- 抓取请求固定走本地 HTTP 代理 127.0.0.1:10808（可用 CLAW_PROXY 覆盖）
- 数据与缩略图缓存存放在项目目录 data/ 下
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 先加载项目根目录的 .env（已存在的环境变量优先，不会被覆盖）
from .envfile import load_env  # noqa: E402 必须在读取下面的配置之前执行

load_env(BASE_DIR / ".env")
DATA_DIR = Path(os.environ.get("CLAW_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("CLAW_DB", DATA_DIR / "gallery.sqlite3"))
CACHE_DIR = DATA_DIR / "cache"
THUMB_CACHE_DIR = CACHE_DIR / "thumbs"
# 封面保存目录（项目根目录 cover/），文件名 <gallery_id>.<ext>
COVER_DIR = Path(os.environ.get("CLAW_COVER_DIR", BASE_DIR / "cover"))
# 下载文件保存目录（项目根目录 downloads/）
DOWNLOADS_DIR = Path(os.environ.get("CLAW_DOWNLOADS_DIR", BASE_DIR / "download"))
# 批量下载时每个文件之间的间隔（秒）
DOWNLOAD_DELAY = float(os.environ.get("CLAW_DOWNLOAD_DELAY", "1.0"))

# 「立即下载」取文件方式（也可在 .env 里配置）：
#   cache  = 先保存到服务器磁盘（download/），再读盘转发给浏览器；
#            重复下载不再请求上游，适合磁盘充足的 VPS
#   stream = 64KB 流式转发，不占服务器磁盘，适合磁盘紧张的 VPS
DOWNLOAD_MODE = (os.environ.get("CLAW_DOWNLOAD_MODE", "stream") or "stream").strip().lower()

# 服务端下载队列的并发任务数（默认 5）
DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("CLAW_DOWNLOAD_CONCURRENCY", "5") or 5))
if DOWNLOAD_MODE not in ("cache", "stream"):
    DOWNLOAD_MODE = "stream"
WEB_DIR = BASE_DIR / "web"
SAMPLES_DIR = BASE_DIR / "samples"

DEFAULT_URL = os.environ.get("CLAW_DEFAULT_URL", "")
# 上游 API 站点地址：留空则从最近一次抓取的列表页 URL 推导
API_ORIGIN = os.environ.get("CLAW_API_ORIGIN", "").strip()

# 固定使用本地 10808 端口的 HTTP 代理；设置 CLAW_PROXY="" 可禁用代理。
DEFAULT_PROXY = "http://127.0.0.1:10808"
_PROXY_ENV = os.environ.get("CLAW_PROXY")
PROXY = (_PROXY_ENV if _PROXY_ENV is not None else DEFAULT_PROXY).strip()

# 运行时可切换的代理开关（由前端「开启代理」按钮控制，并持久化到数据库）
# True = 使用上面的代理地址；False = 直连（适合云服务器）
PROXY_ENABLED = bool(PROXY)


def set_proxy_enabled(enabled: bool) -> None:
    """设置是否使用代理（仅影响后续新建的请求）。"""
    global PROXY_ENABLED
    PROXY_ENABLED = bool(enabled) and bool(PROXY)


def effective_proxy() -> str:
    """当前生效的代理地址；返回空字符串表示直连。"""
    return PROXY if PROXY_ENABLED else ""

USER_AGENT = os.environ.get("CLAW_UA") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 claw-gallery/0.1"
)

REQUEST_TIMEOUT = float(os.environ.get("CLAW_TIMEOUT", "30"))
REQUEST_RETRIES = int(os.environ.get("CLAW_RETRIES", "3"))
PAGE_DELAY = float(os.environ.get("CLAW_DELAY", "1.2"))
MIN_PAGE_DELAY = 0.2
# 详情补全（/api/v2/galleries/{id}）的请求间隔与默认开关
DETAIL_DELAY = float(os.environ.get("CLAW_DETAIL_DELAY", "0.4"))
# 封面下载间隔（秒）与默认开关
COVER_DELAY = float(os.environ.get("CLAW_COVER_DELAY", "0.25"))
COVER_DEFAULT = os.environ.get("CLAW_COVERS", "1").lower() not in ("0", "false", "no")
ENRICH_DEFAULT = os.environ.get("CLAW_ENRICH", "1").lower() not in ("0", "false", "no")
MAX_PAGES = int(os.environ.get("CLAW_MAX_PAGES", "200"))
# 增量抓取默认开关：同一列表地址再次抓取时，只抓新增条目（遇到已收录条目即停）
INCREMENTAL = os.environ.get("CLAW_INCREMENTAL", "1").lower() not in ("0", "false", "no")
# 列表页满页条目数（明显少于该值时通常说明已到末页）
PAGE_SIZE_HINT = int(os.environ.get("CLAW_PAGE_SIZE_HINT", "20"))
# 若本地代理对 https 站点做 MITM 导致证书报错，可设置 CLAW_INSECURE_TLS=1 关闭校验
INSECURE_TLS = os.environ.get("CLAW_INSECURE_TLS", "").lower() in {"1", "true", "yes"}

HOST = os.environ.get("CLAW_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAW_PORT", "8787"))

THUMB_MAX_BYTES = 8 * 1024 * 1024
EXPORT_MAX_ROWS = 20000

# 列表页的 /language/<slug>/ 标签 -> 展示名
LANGUAGE_LABELS = {
    "english": "English",
    "japanese": "日本語",
    "chinese": "中文",
    "korean": "한국어",
    "spanish": "Español",
    "portuguese": "Português",
    "french": "Français",
    "german": "Deutsch",
    "russian": "Русский",
    "italian": "Italiano",
    "thai": "ไทย",
    "vietnamese": "Tiếng Việt",
    "indonesian": "Bahasa Indonesia",
    "malay": "Bahasa Melayu",
    "tagalog": "Tagalog",
    "polish": "Polski",
    "ukrainian": "Українська",
    "turkish": "Türkçe",
    "arabic": "العربية",
    "dutch": "Nederlands",
    "czech": "Čeština",
    "hungarian": "Magyar",
    "romanian": "Română",
    "greek": "Ελληνικά",
    "swedish": "Svenska",
    "danish": "Dansk",
    "finnish": "Suomi",
    "norwegian": "Norsk",
    "bulgarian": "Български",
    "croatian": "Hrvatski",
    "serbian": "Српски",
    "slovak": "Slovenčina",
    "slovenian": "Slovenščina",
    "hebrew": "עברית",
    "hindi": "हिन्दी",
    "catalan": "Català",
    "estonian": "Eesti",
    "latvian": "Latviešu",
    "lithuanian": "Lietuvių",
    "albanian": "Shqip",
    "esperanto": "Esperanto",
    "galician": "Galego",
    "unknown": "未标注",
}

# 列表页卡片用 lang-xx class 标记语言（如 <div class="gallery lang-gb">）
LANGUAGE_CLASS_MAP = {
    "gb": "english", "uk": "ukrainian", "us": "english", "en": "english",
    "jp": "japanese", "ja": "japanese", "cn": "chinese", "zh": "chinese",
    "kr": "korean", "ko": "korean", "es": "spanish", "mx": "spanish",
    "br": "portuguese", "pt": "portuguese", "fr": "french", "de": "german",
    "ru": "russian", "it": "italian", "th": "thai", "vi": "vietnamese",
    "id": "indonesian", "ms": "malay", "tl": "tagalog", "ph": "tagalog",
    "pl": "polish", "tr": "turkish", "nl": "dutch", "cs": "czech",
    "hu": "hungarian", "ro": "romanian", "el": "greek", "sv": "swedish",
    "da": "danish", "fi": "finnish", "no": "norwegian", "nb": "norwegian",
    "bg": "bulgarian", "hr": "croatian", "sr": "serbian", "sk": "slovak",
    "sl": "slovenian", "he": "hebrew", "hi": "hindi", "ca": "catalan",
    "et": "estonian", "lv": "latvian", "lt": "lithuanian", "sq": "albanian",
    "eo": "esperanto", "gl": "galician", "ar": "arabic", "fa": "persian",
    "ka": "georgian", "az": "azerbaijani", "hy": "armenian", "mn": "mongolian",
    "ne": "nepali", "si": "sinhala", "ur": "urdu", "bn": "bengali",
    "ta": "tamil", "te": "telugu", "kk": "kazakh", "be": "belarusian",
    "bs": "bosnian", "mk": "macedonian", "is": "icelandic", "ga": "irish",
    "cy": "welsh", "eu": "basque", "mt": "maltese", "af": "afrikaans",
    "sw": "swahili", "la": "latin", "ku": "kurdish", "jv": "javanese",
    "su": "sundanese", "my": "burmese", "km": "khmer", "lo": "lao",
    "ps": "pashto", "sd": "sindhi", "am": "amharic", "yo": "yoruba",
    "ig": "igbo", "zu": "zulu", "ha": "hausa", "so": "somali",
    "tt": "tatar", "uz": "uzbek", "ky": "kyrgyz", "tg": "tajik",
    "tk": "turkmen", "ug": "uyghur",
}
DEFAULT_LANGUAGE_ORDER = ("japanese", "chinese", "english")

# 选版/排序的语言优先级（用户要求：日语 > 中文 > 英文 > 其他）
LANGUAGE_PRIORITY = {"japanese": 0, "chinese": 1, "english": 2}


def language_label(slug: str) -> str:
    """把语言 slug（如 english）转换成展示名。"""
    slug = (slug or "").strip().lower()
    if not slug:
        return LANGUAGE_LABELS["unknown"]
    return LANGUAGE_LABELS.get(slug, slug.replace("-", " ").title())