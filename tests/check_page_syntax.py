"""页面脚本语法检查：直接从 HTML 抽取内联 `<script>`，不再维护手工副本。

历史做法是把页面脚本复制一份成 `_stage_d_syntax.js` 再 `node --check`，
但副本会随 HTML 改动而漂移（2026-10-15 实测已差 1100+ 字符）。
这里改成每次现抽现检，从根上消除漂移。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


def main() -> int:
    html = (ROOT / "中转站比价.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        print("FAIL  在 HTML 里没找到内联 <script>")
        return 1
    failures = 0
    for index, block in enumerate(blocks):
        if not block.strip():
            continue
        process = subprocess.run([NODE, "--check", "-"], input=block, capture_output=True, text=True, encoding="utf-8")
        label = f"内联脚本 #{index + 1}（{len(block)} 字符）"
        if process.returncode == 0:
            print(f"PASS  {label} 语法通过")
        else:
            failures += 1
            print(f"FAIL  {label}\n{process.stderr.strip()}")
    print(f"\n{'全部通过' if not failures else str(failures) + ' 个脚本有语法错误'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
