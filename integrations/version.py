"""版本号读取。

版本文件是发布与健康检查的唯一来源；读不到时只返回可识别的占位值，
不能让首页或 /healthz 因为缺少文件而启动失败。
"""
from __future__ import annotations

import re
from pathlib import Path

FALLBACK_VERSION = "0.0.0-unknown"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-\d+)?$")


def read_version(root: str | Path) -> str:
    """读取 root/VERSION；缺失、不可读或格式不合法时返回占位版本。"""
    try:
        text = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return FALLBACK_VERSION
    return text if _VERSION_RE.match(text) else FALLBACK_VERSION
