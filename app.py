'HOKWorld 主程序启动入口。'
from __future__ import annotations

import os
import sys

if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app_ui.main_window import (
    FishingInterface,  # noqa: F401 - 保留既有测试和扩展导入入口
    MainWindow,
    _nav_icon,
    build_window,
)
from winenv import center_window, hide_console, is_admin, relaunch_as_admin, set_app_id
from runtime_cleanup import cleanup_runtime_artifacts
from runtime_guard import dev_log


def _present_main_window(win: MainWindow) -> None:
    '启动后只将主窗口显示到前台一次。'
    if not win.isVisible() or win.isMinimized():
        win.showNormal()
    if win.isActiveWindow():
        dev_log("主窗口启动前台显示:Qt 已激活")
        return
    try:
        win.raise_()
    except Exception as exc:
        dev_log("Qt 主窗口启动置顶失败", exc)
    try:
        import ctypes
        hwnd = int(win.winId())
        user32 = ctypes.windll.user32
        user32.ShowWindowAsync(ctypes.c_void_p(hwnd), 9 if win.isMinimized() else 5)
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(0), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0040)
        ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
        dev_log(f"主窗口启动前台显示:{'成功' if ok else '系统拒绝'} hwnd={hwnd}")
    except Exception as exc:
        dev_log("Windows 主窗口启动激活失败", exc)


def main() -> int:
    '完成提权、清理和 Qt 窗口启动。'
    hide_console()
    if not is_admin():
        try:
            relaunch_as_admin()
        except Exception as exc:
            dev_log("主程序提权重启失败", exc)
            raise
        return 0
    dev_log(f"主程序启动:pid={os.getpid()} admin=True python={sys.executable}")
    removed, cleanup_errors = cleanup_runtime_artifacts()
    if removed:
        dev_log(f"安装版运行残留已清理:{', '.join(removed)}")
    for error in cleanup_errors:
        dev_log(f"安装版运行残留清理失败:{error}")
    set_app_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(_nav_icon("app.png", QIcon()))
    win = build_window()
    center_window(win)
    win.showNormal()
    dev_log(f"主窗口已创建:hwnd={int(win.winId())}")
    QTimer.singleShot(0, lambda: _present_main_window(win))
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        dev_log("主程序启动致命异常", exc)
        raise
