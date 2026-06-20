# -*- mode: python ; coding: utf-8 -*-
# HOKWorld PyInstaller 打包配置(onedir / 无控制台 / 管理员清单 / 王者徽标图标)。
# 用法:  pyinstaller --noconfirm HOKWorld.spec
# 产物:  dist\HOKWorld\HOKWorld.exe(连同 _internal 资源目录,整目录由 Inno 打成安装包)
from PyInstaller.utils.hooks import collect_all

# 随程序分发的只读资源:界面/exe 图标 + 钓鱼识别模板(冻结后位于 _internal\ 下,
# 由 paths.resource_path() 经 sys._MEIPASS 定位)
datas = [("assets", "assets"), ("fishing/templates", "fishing/templates")]
binaries = []
hiddenimports = ["win32gui", "win32api", "win32con"]

# OCR / 运行时(含 .onnx 模型与原生 DLL)、Fluent 控件资源(qss/字体/图标)整包收集
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "qfluentwidgets"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2",
              "pytest", "_pytest", "scipy", "pandas", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HOKWorld",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # 无控制台,只出 UI
    disable_windowed_traceback=False,
    icon="assets/app.ico",    # exe / 任务栏 / 快捷方式图标(王者徽标)
    uac_admin=True,           # 自带管理员清单 → UAC 显示 HOKWorld.exe(游戏提权,发输入需管理员)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HOKWorld",
)
