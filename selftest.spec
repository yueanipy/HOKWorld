# -*- mode: python ; coding: utf-8 -*-
# 冻结自检构建:与 HOKWorld.spec 相同的资源/依赖收集,但入口=selftest.py、
# 带控制台、无管理员清单,便于在无显示/无游戏环境下自动跑通。
# 用法: pyinstaller --noconfirm selftest.spec  ->  dist\HOKWorld-selftest\HOKWorld-selftest.exe
from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets"), ("fishing/templates", "fishing/templates")]
binaries = []
hiddenimports = ["win32gui", "win32api", "win32con"]
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "qfluentwidgets"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["selftest.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2",
              "pytest", "_pytest", "scipy", "pandas", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="HOKWorld-selftest",
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="HOKWorld-selftest")
