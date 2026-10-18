"""文件名生成与净化。

下载到磁盘的文件统一命名为 `{作品ID}_{作品名称}.{扩展名}`。

需要处理的合规性问题：
- Windows/Linux 非法字符与控制字符（尖括号、冒号、斜杠、竖线、问号、星号等）
- Windows 不允许文件名以「点」或「空格」结尾
- Windows 保留设备名（CON / PRN / AUX / NUL / COM1-9 / LPT1-9）
- 文件系统长度限制（单段名过长会导致保存失败）
- 标题可能为空或全是非法字符，需要兜底
"""
from __future__ import annotations

import re
import unicodedata

# 非法字符与控制字符
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# 连续空白
_SPACES_RE = re.compile(r"\s+")

# Windows 保留设备名（不区分大小写，带扩展名也算）
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
_RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}

DEFAULT_MAX_LENGTH = 120


def sanitize_filename(text: str, *, max_length: int = DEFAULT_MAX_LENGTH,
                      fallback: str = "untitled") -> str:
    """把任意文本净化成安全的文件名片段（不含扩展名）。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _ILLEGAL_RE.sub(" ", value)          # 非法字符换成空格
    value = _SPACES_RE.sub(" ", value).strip()   # 折叠空白
    value = value.strip(". ")                    # 不允许以点/空格结尾
    if not value:
        value = fallback
    if value.upper() in _RESERVED_NAMES:         # 避开保留设备名
        value = f"{value}_file"
    if len(value) > max_length:
        value = value[:max_length].rstrip(". ")
        if not value:
            value = fallback
    return value


def download_filename(gallery_id: int, title: str, extension: str,
                      *, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """生成 `{id}_{名称}.{ext}` 形式的下载文件名。"""
    gid = int(gallery_id)
    ext = re.sub(r"[^0-9a-zA-Z]", "", str(extension or "zip")).lower() or "bin"
    # 标题先单独净化并按剩余长度截断，避免超长标题把 id 段挤掉
    room = max(8, max_length - len(str(gid)) - 2)
    title_part = sanitize_filename(title, max_length=room, fallback="").strip()
    stem = f"{gid}_{title_part}" if title_part else str(gid)
    return f"{stem}.{ext}"