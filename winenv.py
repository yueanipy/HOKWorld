"""Windows 运行环境与游戏窗口工具:管理员/提权、隐藏控制台、DPI 感知、窗口居中、
《王者荣耀世界》窗口枚举与客户区坐标。供 app 与各识别任务共用(纯黑盒,只读窗口信息)。"""
from __future__ import annotations

import ctypes
import os
import sys

import win32gui

GAME_TITLE = "王者荣耀世界"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _pythonw() -> str:
    """pythonw.exe(无控制台);找不到则回退当前解释器。冻结成 exe 时直接用自身。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def relaunch_as_admin() -> None:
    """以管理员重启自身(游戏多为提权进程,普通权限发的键鼠会被 UIPI 拦截)。"""
    if getattr(sys, "frozen", False):
        # 已打包:重启 exe 本身,exe 自带管理员清单 → UAC 显示程序名
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, "", None, 1)
        return
    script = os.path.abspath(sys.argv[0])     # 入口脚本用 argv[0],不能用 __file__
    ctypes.windll.shell32.ShellExecuteW(None, "runas", _pythonw(), f'"{script}"', None, 1)


def hide_console() -> None:
    """隐藏当前进程的控制台窗口(若有),只留 UI。"""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def set_dpi_awareness() -> None:
    """声明 Per-Monitor-V2 DPI 感知,使 win32 客户区坐标与 mss 截屏在任意缩放下口径一致。
    必须早于任何窗口/坐标/截屏调用。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Win10 1703+
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Win8.1
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()    # 老系统兜底
            except Exception:
                pass


# import 本模块即声明,确保早于 Qt / win32 / mss
set_dpi_awareness()


def center_window(win) -> None:
    """把窗口移到光标所在显示器可用区域正中(任意分辨率/缩放)。"""
    try:
        from PySide6.QtGui import QCursor, QGuiApplication
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - win.width()) // 2
        y = geo.y() + (geo.height() - win.height()) // 2
        win.move(max(geo.x(), x), max(geo.y(), y))
    except Exception:
        pass


def find_game_hwnd() -> int | None:
    found: list[int] = []

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == GAME_TITLE:
            found.append(h)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    l, t, r, b = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (l, t))
    right, bottom = win32gui.ClientToScreen(hwnd, (r, b))
    return left, top, right - left, bottom - top


def is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and win32gui.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd)
