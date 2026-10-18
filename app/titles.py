"""标题归一化与「是否同一作品」判定的公共逻辑（列表页 / 详情页共用）。

分层策略：
1. 把标题拆成「普通文本」和「括号段」；
2. 识别括号段里的噪声（语言、汉化组、Digital、C107 场次等）；
3. 用去噪后的核心文本做精确分组；
4. 兜底用「卷号签名 + 前缀/相似度」做保守模糊合并（宁可少合并，不可误合并）。
"""
from __future__ import annotations

import difflib
import re
import unicodedata

BRACKET_RE = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)|（([^）]*)）|【([^】]*)】|《([^》]*)》")
VOLUME_RE = re.compile(
    r"(?:vol(?:ume)?|chap(?:ter)?|ch|part|卷|話|话|集)\s*\.?\s*(\d{1,4})",
    re.IGNORECASE,
)
TRAILING_NUMBER_RE = re.compile(r"(?<![0-9a-z])(\d{1,4})\s*$", re.IGNORECASE)

# 括号里属于「语言 / 汉化组 / 格式」的噪声，不算标题内容
NOISE_TOKENS = {
    "english", "chinese", "japanese", "korean", "spanish", "portuguese", "french",
    "german", "russian", "italian", "thai", "vietnamese", "indonesian", "polish",
    "turkish", "arabic", "dutch", "ukrainian", "czech", "hungarian", "romanian",
    "greek", "swedish", "danish", "finnish", "norwegian", "bulgarian", "croatian",
    "serbian", "slovak", "slovenian", "hebrew", "hindi", "malay", "tagalog",
    "catalan", "estonian", "latvian", "lithuanian", "albanian", "esperanto",
    "galician", "digital", "ebook", "uncensored", "censored", "decensored",
    "colorized", "colored", "fullcolor", "incomplete", "complete", "sample",
    "preview", "wip", "translated", "translation", "mtl", "tl", "tls", "raw",
    "raws", "web", "scan", "scans", "textcleaned", "rewrite", "speechless",
    "ai", "ai翻译", "ai汉化", "中文版", "日文版", "英文版",
}
NOISE_HINTS = (
    "汉化", "漢化", "翻译", "翻譯", "翻訳", "嵌字", "修图", "校对", "校對",
    "subs", "scans", "tls", "mtl", "translator", "translated by",
)


def normalize_text(value: str) -> str:
    """NFKC + 小写 + 去掉标点/空白（保留中日韩文字与数字）。"""
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def split_brackets(title: str) -> list[tuple[str, str]]:
    """把标题拆成 ("text"/"bracket", 内容) 交替序列。"""
    value = unicodedata.normalize("NFKC", title or "")
    segments: list[tuple[str, str]] = []
    cursor = 0
    for match in BRACKET_RE.finditer(value):
        if match.start() > cursor:
            segments.append(("text", value[cursor:match.start()]))
        segments.append(("bracket", match.group(0)[1:-1]))
        cursor = match.end()
    if cursor < len(value):
        segments.append(("text", value[cursor:]))
    return segments


def is_noise_bracket(value: str) -> bool:
    """判断括号段是不是语言/汉化组/格式等噪声（不是标题内容）。"""
    lowered = (value or "").strip().casefold()
    if not lowered:
        return True
    compact = re.sub(r"[\s\-_/·]+", "", lowered)
    if compact in NOISE_TOKENS:
        return True
    if re.fullmatch(r"[a-z]{0,3}\d{2,4}", compact):   # C107 / W2020 等活动场次编号
        return True
    if re.fullmatch(r"\d{1,4}", compact):             # 纯数字
        return True
    if "訳" in lowered:                               # 翻訳 / 英訳 / 中国翻訳
        return True
    if "版" in lowered and len(compact) <= 6:         # DL版 / 書換え版 / 中文版
        return True
    return any(hint in lowered for hint in NOISE_HINTS)


def title_meta(title: str) -> dict:
    """标题指纹：核心文本 / 纯文本 / 非噪声括号集合 / 卷号签名。"""
    core_parts: list[str] = []
    plain_parts: list[str] = []
    brackets: list[str] = []
    for kind, value in split_brackets(title):
        if kind == "bracket":
            if is_noise_bracket(value):
                continue
            brackets.append(normalize_text(value))
            core_parts.append(value)
        else:
            core_parts.append(value)
            plain_parts.append(value)
    volume_text = " ".join(plain_parts)
    volumes = set(VOLUME_RE.findall(volume_text))
    trailing = TRAILING_NUMBER_RE.search(volume_text.strip())
    if trailing:
        volumes.add(trailing.group(1))
    return {
        "core": normalize_text(" ".join(core_parts)),
        "plain": normalize_text(volume_text),
        "brackets": frozenset(item for item in brackets if item),
        "volumes": frozenset(item.lstrip("0") or "0" for item in volumes),
    }


def longest_common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def similar_meta(left: dict, right: dict, *, prefix_ratio: float = 0.75,
                 ratio_threshold: float = 0.90) -> bool:
    """保守判断两份标题指纹是否属于同一作品。"""
    if left["volumes"] != right["volumes"]:
        return False  # Vol.2 与 Vol.3 只差一个字符，必须靠卷号签名区分
    left_brackets, right_brackets = left["brackets"], right["brackets"]
    if not (left_brackets <= right_brackets or right_brackets <= left_brackets):
        return False  # 原作名/系列名等括号内容互相冲突 -> 不是同一作品
    core_a, core_b = left["core"], right["core"]
    if not core_a or not core_b:
        return False
    if core_a == core_b:
        return True
    shorter, longer = (core_a, core_b) if len(core_a) <= len(core_b) else (core_b, core_a)
    if len(shorter) < 16:
        return False
    prefix = longest_common_prefix(core_a, core_b)
    if prefix >= max(20, prefix_ratio * len(shorter)):
        return True  # 一个是另一个的前缀：副标题 / 汉化组 / 场次编号等差异
    ratio = difflib.SequenceMatcher(None, core_a, core_b).ratio()
    return ratio >= ratio_threshold and len(shorter) / len(longer) >= 0.75