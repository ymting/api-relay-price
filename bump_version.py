#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本号维护工具：dev 递增与发版定版。

规则（与发布流程一致，VERSION 是唯一来源）：
    生产当前 1.0.0  →  dev 依次 1.0.1-1、1.0.1-2 …
    dev 的 1.0.1-3  →  发版定版为 1.0.1
    正式发布之后    →  dev 重新从 1.0.2-1 开始

用法：
    python bump_version.py            # 显示当前版本与建议的下一步
    python bump_version.py --dev      # dev 递增，提交前调用
    python bump_version.py --release  # 去掉 -n 后缀，准备合并发版
    python bump_version.py --dev --dry-run   # 只看结果，不写文件

只改 VERSION 文件；不改 tag、不提交、不推送。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"

_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_DEV_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(\d+)$")


def parse(text):
    """拆版本号，返回 (major, minor, patch, dev序号)；正式版 dev 为 None。"""
    text = (text or "").strip()
    match = _DEV_RE.match(text)
    if match:
        return int(match[1]), int(match[2]), int(match[3]), int(match[4])
    match = _RELEASE_RE.match(text)
    if match:
        return int(match[1]), int(match[2]), int(match[3]), None
    raise ValueError(f"无法识别的版本号：{text!r}（应形如 1.2.3 或 1.2.3-1）")


def next_dev(current):
    """dev 递增：已有 -n 就 n+1；正式版则 patch+1 后从 -1 起。"""
    major, minor, patch, dev = parse(current)
    if dev is None:
        return f"{major}.{minor}.{patch + 1}-1"
    return f"{major}.{minor}.{patch}-{dev + 1}"


def release_of(current):
    """定版：去掉 -n 后缀。已经是正式版则报错，避免误操作。"""
    major, minor, patch, dev = parse(current)
    if dev is None:
        raise ValueError(f"{current} 已经是正式版本，不需要定版")
    return f"{major}.{minor}.{patch}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="维护 VERSION：dev 递增或发版定版")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dev", action="store_true", help="dev 递增：1.0.1-2 → 1.0.1-3")
    mode.add_argument("--release", action="store_true", help="去掉 -n：1.0.1-3 → 1.0.1")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args(argv)

    try:
        current = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"读取 VERSION 失败：{error}", file=sys.stderr)
        return 1

    try:
        if args.dev:
            target = next_dev(current)
        elif args.release:
            target = release_of(current)
        else:
            _, _, _, dev = parse(current)
            hint = release_of(current) if dev is not None else next_dev(current)
            print(f"当前版本：{current}")
            print(f"提示：{'发版定版为' if dev is not None else '下一个 dev 版本为'} {hint}")
            return 0
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"{current} → {target}（未写入）")
        return 0

    VERSION_FILE.write_text(target + "\n", encoding="utf-8")
    print(f"{current} → {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
