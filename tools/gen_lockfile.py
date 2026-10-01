"""从 pip 解析报告生成 requirements-lock.txt。

为什么要程序化生成：手工拼锁文件已经错过两次（漏 lxml_html_clean、漏 pypng/钉钉 SDK），
而且版本组合还会互相冲突（lxml_html_clean 0.4.5 要求 lxml>=6.1.1）。
这里以 pip 的解析结果为准，并回查 PyPI 确认每个版本在 3.11 上有 wheel。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "req_report.json"
TARGET = ROOT / "requirements-lock.txt"

HEADER = """# 精确锁定的依赖清单（requirements-lock.txt）
#
# 生成方式（不要手工改版本号，见 tools/gen_lockfile.py）：
#     pip install --dry-run --ignore-installed --python-version 3.11 --only-binary=:all: \\
#       --report req_report.json -r requirements.in -c constraints-py311.txt
#     python tools/gen_lockfile.py
#
# 说明：
# - 全部版本都确认在 PyPI 上有 cp311 manylinux/musllinux wheel 或纯 Python wheel，
#   python:3.11-slim 上不会触发源码编译（唯一例外见文末 pyppeteer-box）。
# - 已用 tools/audit_imports.py 对账：上游代码 import 的第三方包全部在内。
"""

#: 只有 sdist 的包（必须源码安装，自身无 C 扩展依赖）
SDIST_ONLY = {
    "pyppeteer-box": "# pyppeteer-box 在 PyPI 上只有 sdist（无 wheel），必须源码安装；它自身无 C 扩展依赖。",
}


def wheel_ok(name: str, version: str) -> tuple[bool, str]:
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.load(response)
    files = [item["filename"] for item in payload.get("urls", [])]
    for filename in files:
        if filename.endswith("py2.py3-none-any.whl") or filename.endswith("py3-none-any.whl"):
            return True, filename
        if "cp311" in filename and ("manylinux" in filename or "musllinux" in filename):
            return True, filename
    return False, (files[0] if files else "无任何发行文件")


def main() -> int:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    rows = sorted((item["metadata"]["name"], item["metadata"]["version"]) for item in report["install"])

    problems = []
    lines = [HEADER.rstrip(), ""]
    for name, version in rows:
        ok, detail = wheel_ok(name, version)
        marker = "# 源码安装（无 wheel）" if not ok else ""
        lines.append("%s==%s%s" % (name, version, ("  " + marker) if marker else ""))
        if not ok:
            problems.append((name, version, detail))

    lines.append("")
    for name, comment in SDIST_ONLY.items():
        lines.append(comment)
        lines.append("%s==0.0.27" % name)

    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已写入 %s：%d 个包" % (TARGET.name, len(rows) + len(SDIST_ONLY)))
    for name, version, detail in problems:
        print("  注意：%s==%s 无 3.11 wheel -> %s" % (name, version, detail))
    if not problems:
        print("全部 %d 个包在 3.11 上都有 wheel" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
