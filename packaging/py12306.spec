# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把桌面程序打成 exe。

用法（Windows）:
    .venv/Scripts/pyinstaller packaging/py12306.spec --noconfirm
产物:
    dist/py12306/py12306.exe

要点：
- 面板 UI、图标、上游 Web 前端产物、data 目录都要一起打进去（否则界面白屏/缺图标）；
- console=False：窗口程序没有控制台，所以 app.py 里装了崩溃处理器（写日志 + 弹窗）。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - PyInstaller 注入

datas = [
    (str(ROOT / "webpanel" / "ui"), "webpanel/ui"),
    (str(ROOT / "py12306" / "web" / "static"), "py12306/web/static"),
    (str(ROOT / "data"), "data"),
]
example = ROOT / ".env.example"
if example.is_file():
    datas.append((str(example), "."))

icon = ROOT / "packaging" / "app_icon.ico"
if not icon.is_file():
    icon = None

# exe 文件属性（右键 -> 属性 -> 详细信息）；版本号由 packaging/sync_version.py 同步
version_file = ROOT / "packaging" / "version_info.txt"
if not version_file.is_file():
    version_file = None

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pyqtgraph",
    "numpy",
    "flask",
    "flask_jwt_extended",
    "requests_html",
    "py12306.app",
    "py12306.config",
    "webpanel.server",
    "core.engine",
    "core.uplink",
    "core.monitor",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQml",
        "matplotlib",
        "scipy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="py12306",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon) if icon else None,
    version=str(version_file) if version_file else None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="py12306",
)
