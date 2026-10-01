# -*- coding: utf-8 -*-
"""把 core/version.py 里的版本号同步到 packaging/version_info.txt。

CI 在 PyInstaller 打包前调用；本地也可以手动跑。
用法：python packaging/sync_version.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 cp1252，下面的中文输出会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.version import APP_NAME, APP_NAME_EN, __version__  # noqa: E402

TARGET = Path(__file__).resolve().parent / "version_info.txt"


def main() -> int:
    parts = [int(piece) for piece in re.findall(r"\d+", __version__)[:3]]
    while len(parts) < 3:
        parts.append(0)
    vers = tuple(parts) + (0,)

    text = TARGET.read_text(encoding="utf-8")
    text = re.sub(r"filevers=\([^)]*\)", "filevers=%r" % (vers,), text)
    text = re.sub(r"prodvers=\([^)]*\)", "prodvers=%r" % (vers,), text)
    text = re.sub(r"StringStruct\('FileVersion', '[^']*'\)", "StringStruct('FileVersion', '%s')" % __version__, text)
    text = re.sub(
        r"StringStruct\('ProductVersion', '[^']*'\)",
        "StringStruct('ProductVersion', '%s')" % __version__,
        text,
    )
    text = re.sub(r"StringStruct\('ProductName', '[^']*'\)", "StringStruct('ProductName', '%s')" % APP_NAME, text)
    text = re.sub(
        r"StringStruct\('FileDescription', '[^']*'\)",
        "StringStruct('FileDescription', '%s')" % APP_NAME,
        text,
    )
    text = re.sub(r"StringStruct\('InternalName', '[^']*'\)", "StringStruct('InternalName', '%s')" % APP_NAME_EN, text)
    text = re.sub(
        r"StringStruct\('OriginalFilename', '[^']*'\)",
        "StringStruct('OriginalFilename', '%s.exe')" % APP_NAME_EN,
        text,
    )
    TARGET.write_text(text, encoding="utf-8")
    print("version_info.txt 已更新为 %s" % __version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
