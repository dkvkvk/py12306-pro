"""在干净环境里验证：按锁文件装完，上游每个模块都能 import。"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 上游全部模块（含 lxml/requests-html/pypng/钉钉 SDK 等重依赖链）
modules = []
for info in pkgutil.walk_packages([str(ROOT / "py12306")], prefix="py12306."):
    modules.append(info.name)
modules += ["main", "upstream_entry", "settings"]
try:
    import core
    for info in pkgutil.iter_modules(core.__path__):
        modules.append("core." + info.name)
except Exception as exc:
    print("core 导入失败:", exc)

ok, failed = [], []
for name in sorted(set(modules)):
    if name == "main":
        # main.py 只做入口，import 会执行 core.cli（无副作用），可以导入
        pass
    try:
        importlib.import_module(name)
        ok.append(name)
    except Exception as exc:
        failed.append((name, "%s: %s" % (type(exc).__name__, exc)))

print("导入成功 %d 个模块" % len(ok))
print("上游模块:", len([m for m in ok if m.startswith('py12306')]))
if failed:
    print("\n失败 %d 个：" % len(failed))
    for name, err in failed:
        print("  - %s -> %s" % (name, err))
    sys.exit(1)
print("全部模块导入成功")
