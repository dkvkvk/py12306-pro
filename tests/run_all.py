"""跑全部测试：python tests/run_all.py

与参考项目（host_app）同样的零依赖方式：不装 pytest 也能跑，
适合用户机器上快速自查。装了 pytest 的环境更推荐：
    pytest -q --basetemp=.pytest-tmp
"""

from __future__ import annotations

import glob
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

# 界面测试必须离屏跑，否则在无桌面环境会直接失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

failures: list[str] = []
total = 0
skipped: list[str] = []


def _has_pyside6() -> bool:
    try:
        import PySide6  # noqa: F401

        return True
    except Exception:
        return False


for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.splitext(os.path.basename(path))[0]
    if name == "test_gui" and not _has_pyside6():
        skipped.append(name)
        print("\n===== %s ===== 跳过（未安装 PySide6）" % name)
        continue
    print("\n===== %s =====" % name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        traceback.print_exc()
        failures.append(name)
        continue

    tests = [
        (n, f)
        for n, f in sorted(vars(module).items())
        if n.startswith("test_") and callable(f)
    ]
    for test_name, func in tests:
        total += 1
        try:
            # 支持需要 tmp_path 的测试：不用 pytest 时给一个临时目录
            code = func.__code__
            if "tmp_path" in code.co_varnames[: code.co_argcount]:
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as tmp:
                    func(Path(tmp))
            else:
                func()
            print("  PASS %s" % test_name)
        except Exception:
            traceback.print_exc()
            failures.append("%s.%s" % (name, test_name))

print("\n" + "=" * 46)
if skipped:
    print("跳过的测试文件:", ", ".join(skipped))
if failures:
    print("失败的测试 (%d):" % len(failures), ", ".join(failures))
    sys.exit(1)
print("全部测试通过 (ALL PASS)，共 %d 个用例" % total)
