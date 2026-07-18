'Windows 运行环境与游戏窗口工具:管理员/提权、隐藏控制台、DPI 感知、窗口居中、。'
from __future__ import annotations

import ctypes
import os
import sys

import win32gui
import win32process

from runtime_guard import dev_log

GAME_TITLE = "王者荣耀世界"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _pythonw() -> str:
    'pythonw.exe(无控制台);找不到则回退当前解释器。'
    if getattr(sys, "frozen", False):
        return sys.executable
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def relaunch_as_admin() -> None:
    '以管理员重启自身(游戏多为提权进程,普通权限发的键鼠会被 UIPI 拦截)。'
    if getattr(sys, "frozen", False):
        
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, "", None, 1)
        return
    script = os.path.abspath(sys.argv[0])     
    ctypes.windll.shell32.ShellExecuteW(None, "runas", _pythonw(), f'"{script}"', None, 1)


def hide_console() -> None:
    '隐藏当前进程的控制台窗口(若有),只留 UI。'
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   
    except Exception:
        pass


def set_app_id(app_id: str = "yueanipy.HOKWorld") -> None:
    '设置进程 AppUserModelID:让任务栏/Alt+Tab 用窗口图标(app.png)而非 python 宿主图标。'
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def set_dpi_awareness() -> None:
    '声明 Per-Monitor-V2 DPI 感知,使 win32 客户区坐标与 mss 截屏在任意缩放下口径一致。'
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()    
            except Exception:
                pass



set_dpi_awareness()


def center_window(win) -> None:
    '把窗口移到光标所在显示器可用区域正中(任意分辨率/缩放)。'
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


def find_game_hwnd(prefer_foreground: bool = True, exclude_hwnd: int = 0) -> int | None:
    try:
        foreground = win32gui.GetForegroundWindow()
    except Exception:
        foreground = 0
    try:
        if (prefer_foreground and foreground and foreground != exclude_hwnd
                and win32gui.IsWindowVisible(foreground)
                and not win32gui.IsIconic(foreground)
                and win32gui.GetWindowText(foreground).strip() == GAME_TITLE):
            return foreground
    except Exception:
        pass
    try:
        foreground_pid = win32process.GetWindowThreadProcessId(foreground)[1] if foreground else 0
    except Exception:
        foreground_pid = 0
    found: list[tuple[int, int, bool, bool]] = []

    def _cb(h, _):
        if h == exclude_hwnd:
            return
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).strip() == GAME_TITLE:
            try:
                l, t, r, b = win32gui.GetClientRect(h)
                area = max(0, r - l) * max(0, b - t)
                minimized = bool(win32gui.IsIconic(h))
                pid = win32process.GetWindowThreadProcessId(h)[1]
            except Exception:
                area, minimized, pid = 0, True, 0
            found.append((h, area, minimized, bool(foreground_pid and pid == foreground_pid)))

    win32gui.EnumWindows(_cb, None)
    if not found:
        return None
    
    found.sort(key=lambda item: (item[3] if prefer_foreground else False, item[1], not item[2]), reverse=True)
    return found[0][0]


def last_input_tick() -> int | None:
    '返回 Windows 最近一次键鼠输入的系统 tick；读取失败返回 None。'
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        return int(info.dwTime) if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)) else None
    except Exception:
        return None


def allow_foreground_activation() -> bool:
    '在主界面仍有前台资格时，允许随后启动的启动器/游戏取得前台。'
    try:
        return bool(ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF))  
    except Exception as exc:
        dev_log("允许游戏取得前台失败", exc)
        return False


def can_auto_activate_game(input_tick_at_start: int | None) -> bool:
    '仅在用户没有主动切到第三方程序时允许自动把游戏拉到前台。'
    current_tick = last_input_tick()
    if input_tick_at_start is None or current_tick is None or current_tick == input_tick_at_start:
        return True
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return True
        if win32gui.GetWindowText(fg).strip() == GAME_TITLE:
            return True
        if win32gui.GetClassName(fg) in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        import os
        import win32process
        return win32process.GetWindowThreadProcessId(fg)[1] == os.getpid()
    except Exception:
        return False


def activate_game_window(hwnd: int | None = None) -> bool:
    '恢复并激活正式游戏窗口；调用方负责确认没有用户主动切到其他程序。'
    hwnd = int(hwnd or find_game_hwnd() or 0)
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindowAsync(hwnd, 9 if win32gui.IsIconic(hwnd) else 5)  
        allow_foreground_activation()
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            
            pass
        if win32gui.GetForegroundWindow() != hwnd:
            
            foreground = win32gui.GetForegroundWindow()
            current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            foreground_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            target_tid = user32.GetWindowThreadProcessId(hwnd, None)
            attached_foreground = bool(foreground_tid and foreground_tid != current_tid
                                       and user32.AttachThreadInput(current_tid, foreground_tid, True))
            attached_target = bool(target_tid and target_tid != current_tid
                                   and user32.AttachThreadInput(current_tid, target_tid, True))
            try:
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            finally:
                if attached_target:
                    user32.AttachThreadInput(current_tid, target_tid, False)
                if attached_foreground:
                    user32.AttachThreadInput(current_tid, foreground_tid, False)
        return is_foreground(hwnd)
    except Exception as exc:
        dev_log("将游戏切到前台失败", exc)
        return False


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    l, t, r, b = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (l, t))
    right, bottom = win32gui.ClientToScreen(hwnd, (r, b))
    return left, top, right - left, bottom - top


def is_foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            return False
        foreground = win32gui.GetForegroundWindow()
        if foreground == hwnd:
            return True
        
        if foreground and win32gui.GetWindowText(foreground).strip() == GAME_TITLE:
            return False
        
        target_pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        foreground_pid = win32process.GetWindowThreadProcessId(foreground)[1] if foreground else 0
        return bool(target_pid and target_pid == foreground_pid)
    except Exception:
        return False
