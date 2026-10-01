"""对账：上游代码真正 import 的第三方包 vs 锁文件里有没有。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)

# 上游代码里用到的「import 名 -> PyPI 发行名」映射
DIST_ALIASES = {
    "PySide6": "PySide6-Essentials",
    "png": "pypng",
    "bs4": "beautifulsoup4",
    "flask": "Flask",
    "flask_jwt_extended": "Flask-JWT-Extended",
    "jwt": "PyJWT",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "dingtalkchatbot": "DingtalkChatbot",
    "lightpush": "lightpush",
    "pyppeteer": "pyppeteer",
    "requests_html": "requests-html",
    "lxml_html_clean": "lxml_html_clean",
    "fake_useragent": "fake-useragent",
    "pyquery": "pyquery",
    "redis": "redis",
    "requests": "requests",
    "jinja2": "Jinja2",
    "markupsafe": "MarkupSafe",
    "itsdangerous": "itsdangerous",
    "werkzeug": "Werkzeug",
    "click": "click",
    "colorama": "colorama",
    "websockets": "websockets",
    "cryptography": "cryptography",
}

# 本项目自己的包：不算第三方依赖
LOCAL_PREFIXES = ("py12306", "core", "ui", "webpanel", "app")

ROOT_FILES = ["app.py"]
files = sorted(list(ROOT.glob("py12306/**/*.py")) + list(ROOT.glob("core/*.py")) + list(ROOT.glob("ui/*.py")) + list(ROOT.glob("webpanel/*.py")) + [ROOT / name for name in ROOT_FILES])
found: dict[str, set[str]] = {}
for path in files:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print("解析失败", path, exc)
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                top = node.module.split(".")[0]
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))

# 以使用者侧的 requirements.txt 为准做对账（它就是构建镜像时装的清单）
lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
locked = set()
for line in lock.splitlines():
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        continue
    locked.add(line.split("==")[0].strip().lower().replace("_", "-"))

third_party = {
    name: places
    for name, places in found.items()
    if name not in STDLIB and not name.startswith("_") and not name.startswith(LOCAL_PREFIXES)
}

missing = []
for name, places in sorted(third_party.items()):
    dist = DIST_ALIASES.get(name, name)
    normalized = dist.lower().replace("_", "-")
    if normalized not in locked:
        missing.append((name, dist, sorted(places)[:2]))

print("上游第三方 import 共 %d 个" % len(third_party))
if missing:
    print("\n锁文件里缺失（会导致容器启动即崩）：")
    for name, dist, places in missing:
        print("  - import %-22s -> PyPI %-20s 用于 %s" % (name, dist, places))
else:
    print("锁文件已覆盖全部第三方 import")
