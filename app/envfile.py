"""极简 .env 加载器（零第三方依赖）。

规则：
- 每行 `KEY=VALUE`；以 `#` 开头的行是注释；空行忽略
- 值两边可以用单/双引号包裹，会自动去掉
- 已存在的环境变量**不覆盖**（环境变量优先级高于 .env）
- 支持在值后面写 `# 注释`（未加引号时）
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def parse_env_text(text: str) -> dict[str, str]:
    """把 .env 文本解析成字典（纯函数，便于测试）。"""
    result: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        result[key] = value
    return result


def load_env(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """读取 .env 并写入 os.environ；返回实际生效的键值。"""
    target = Path(path) if path else ENV_PATH
    if not target.is_file():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    parsed = parse_env_text(text)
    applied: dict[str, str] = {}
    for key, value in parsed.items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied