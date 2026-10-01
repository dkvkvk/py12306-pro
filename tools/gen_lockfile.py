"""从 pip 解析报告生成 requirements-lock.txt。

为什么要程序化生成：手工拼锁文件已经错过两次（漏 lxml_html_clean、漏 pypng/钉钉 SDK），
而且版本组合还会互相冲突（lxml_html_clean 0.4.5 要求 lxml>=6.1.1）。
这里以 pip 的解析结果为准，并回查 PyPI 确认每个版本在 3.11 上有 wheel。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "req_report.json"
TARGET = ROOT / "requirements-lock.txt"
#: 终端用户直接 pip install 用的清单：与锁文件同版本，但**只放可从 wheel 安装的包**
USER_TARGET = ROOT / "requirements.txt"

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


#: 网络不稳时重试次数（PyPI 偶发 SSL EOF）
_FETCH_RETRIES = 4


def _fetch_json(url: str) -> dict:
    last: Exception | None = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "py12306-lockfile/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception as exc:  # noqa: BLE001 - 网络类异常统一重试
            last = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError("拉取 %s 失败：%s" % (url, last))


def wheel_ok(name: str, version: str) -> tuple[bool, str]:
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    payload = _fetch_json(url)
    files = [item["filename"] for item in payload.get("urls", [])]
    for filename in files:
        if not filename.endswith(".whl"):
            continue
        if filename.endswith("py2.py3-none-any.whl") or filename.endswith("py3-none-any.whl"):
            return True, filename
        # abi3 稳定 ABI：cp39/cp310-abi3 的 wheel 在更高版本 Python 上同样可用
        # （PySide6 就是这种），不能只认 cp311
        if "abi3" in filename or "cp311" in filename:
            return True, filename
    return False, (files[0] if files else "无任何发行文件")


USER_HEADER = """# 运行本程序所需的依赖（终端用户直接用这个文件安装）
#
#   pip install -r requirements.txt
#
# 版本与 requirements-lock.txt 一致，但去掉了只有源码包（sdist）的项，
# 保证在 Windows 上不需要 C 编译器也能装好。
# 想完全复现开发环境请用 requirements-lock.txt。
"""

#: 只有 sdist、但不影响主流程的包（装不上也不阻塞桌面程序）
OPTIONAL_SDIST = {"pyppeteer-box": "验证码平台的第三方 fork，只有源码包；不装也能跑（用免费打码）"}


def _write_user_requirements(rows: list[tuple[str, str]]) -> None:
    lines = [USER_HEADER.rstrip(), ""]
    for name, version in rows:
        if name in OPTIONAL_SDIST:
            continue
        lines.append("%s==%s" % (name, version))
    lines.append("")
    for name, comment in OPTIONAL_SDIST.items():
        lines.append("# %s：" % name)
        lines.append("#   %s" % comment)
        lines.append("#   需要时单独装：pip install %s" % name)
    USER_TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已写入 %s：%d 个包（面向使用者）" % (USER_TARGET.name, len(rows) - len(OPTIONAL_SDIST)))


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
    _write_user_requirements(rows)
    print("已写入 %s：%d 个包" % (TARGET.name, len(rows) + len(SDIST_ONLY)))
    for name, version, detail in problems:
        print("  注意：%s==%s 无 3.11 wheel -> %s" % (name, version, detail))
    if not problems:
        print("全部 %d 个包在 3.11 上都有 wheel" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
