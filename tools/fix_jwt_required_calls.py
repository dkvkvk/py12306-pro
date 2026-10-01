"""适配 Flask-JWT-Extended 4.x：@jwt_required 必须调用（@jwt_required()）。

上游 requirements 写的是 Flask-JWT-Extended==3.15.0，那时 @jwt_required 是「装饰器本体」；
4.x 改成了装饰器工厂，裸用 @jwt_required 会把被装饰函数当成 fn 参数传入，
请求时直接 TypeError: wrapper() missing 1 required positional argument: 'fn'。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = sorted((ROOT / "py12306").rglob("*.py"))
PATTERN = re.compile(r"^(\s*)@jwt_required\s*$", re.MULTILINE)


def main() -> int:
    changed = []
    for path in TARGETS:
        source = path.read_text(encoding="utf-8")
        updated, count = PATTERN.subn(r"\1@jwt_required()", source)
        if count:
            path.write_text(updated, encoding="utf-8")
            changed.append((path.relative_to(ROOT), count))
    print("已把 %d 处 @jwt_required 改成 @jwt_required()：" % sum(c for _, c in changed))
    for path, count in changed:
        print("  - %s (%d)" % (path, count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
