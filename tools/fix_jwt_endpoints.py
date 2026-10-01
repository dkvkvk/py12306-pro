"""给受 jwt_required 保护的路由补上显式 endpoint。

背景：flask_jwt_extended 4.x 起，@jwt_required 不再保留被装饰函数的 __name__
（函数名变成 wrapper），而 Flask 的 endpoint 默认取 __name__ —— 于是同一蓝图下
多个受保护路由的 endpoint 全都叫 "bp.wrapper"，注册第二个就抛
"View function mapping is overwriting an existing endpoint function"，
上游 Web 界面直接起不来。上游 requirements 里写的是 Flask-JWT-Extended==3.15.0，
dependabot 升到 4.7.4 后没有相应适配。

修法：给这些路由显式传 endpoint=，不依赖装饰器保留函数名。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDLERS = sorted((ROOT / "py12306" / "web" / "handler").glob("*.py"))


def route_name(node: ast.FunctionDef) -> str:
    return node.name


def fix_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, str, str]] = []  # (行号-1, 旧行, 新行)

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorators = node.decorator_list
        has_jwt = any(
            (isinstance(d, ast.Name) and d.id == "jwt_required")
            or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "jwt_required")
            for d in decorators
        )
        if not has_jwt:
            continue
        for decorator in decorators:
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
                continue
            if decorator.func.attr != "route":
                continue
            already = any(kw.arg == "endpoint" for kw in decorator.keywords)
            if already:
                continue
            index = decorator.lineno - 1
            old_line = lines[index]
            stripped = old_line.rstrip("\n")
            if not stripped.rstrip().endswith(")"):
                raise SystemExit("路由装饰器不是单行，需要手工处理: %s:%d" % (path, decorator.lineno))
            new_line = stripped[:-1].rstrip() + ', endpoint="%s")' % route_name(node)
            if old_line.endswith("\n"):
                new_line += "\n"
            edits.append((index, old_line, new_line))

    for index, old_line, new_line in sorted(edits, reverse=True):
        lines[index] = new_line
    if edits:
        path.write_text("".join(lines), encoding="utf-8")
    return ["%s:%s" % (path.name, new_line.strip()) for _, _, new_line in sorted(edits)]


def main() -> int:
    total = []
    for path in HANDLERS:
        fixed = fix_file(path)
        total.extend(fixed)
    print("已补充 %d 个 endpoint 声明：" % len(total))
    for item in total:
        print("  -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
