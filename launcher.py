'自动启动游戏，通过识别当前界面推进启动器和游戏登录流程。'
from __future__ import annotations

import ctypes
import os
import random
import subprocess
import time
import winreg

import cv2
import numpy as np
import win32con
import win32gui
import win32ui

from capture import GameCapture
from fishing.matcher import _get_ocr
from daily.tasks.monthly_card import read_monthly_card
from winenv import activate_game_window, can_auto_activate_game, is_foreground
from runtime_guard import (dev_log, input_allowed, release_known_keys,
                           safe_click_norm, safe_press_key)

NORM_W = 1920                       
GAME_TITLE_KEY = "王者荣耀世界"      
GAME_EXE_NAME = "王者荣耀世界.exe"   








def _safe_listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _find_exe_under(root):
    '在 root 及其(版本号)子目录、以及同级目录里找 王者荣耀世界.exe,取最新修改的(应对旧版本目录残留)。'
    if not root:
        return None
    root = str(root).strip().strip('"').rstrip("\\/")
    dirs = []
    if os.path.isdir(root):
        dirs.append(root)
        dirs += [os.path.join(root, s) for s in _safe_listdir(root)]       
        parent = os.path.dirname(root)
        if os.path.isdir(parent):
            dirs += [os.path.join(parent, s) for s in _safe_listdir(parent)]  
    cands = [os.path.join(d, GAME_EXE_NAME) for d in dirs]
    cands = [p for p in cands if os.path.isfile(p)]
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def _reg_get(key, name):
    try:
        v, _ = winreg.QueryValueEx(key, name)
        return v
    except OSError:
        return None


def _exe_from_config():
    try:
        from config import cfg
        p = cfg.get("game_path")
        if p and os.path.isfile(p):
            return p
    except Exception as exc:
        dev_log("读取 game_path 配置失败", exc)
    return None


def _exe_from_registry():
    '遍历卸载项,DisplayName 含「王者荣耀世界」→ DisplayIcon(去图标索引)优先,其次 InstallLocation 找 exe。'
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    for hive, sub, flag in roots:
        try:
            base = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(base)[0]):
                try:
                    k = winreg.OpenKey(base, winreg.EnumKey(base, i))
                except OSError:
                    continue
                try:
                    disp = _reg_get(k, "DisplayName")
                    if not disp or GAME_TITLE_KEY not in disp:
                        continue
                    icon = _reg_get(k, "DisplayIcon")
                    if icon:
                        icon = str(icon).split(",")[0].strip().strip('"')
                        if icon.lower().endswith(".exe") and os.path.isfile(icon):
                            return icon
                    p = _find_exe_under(_reg_get(k, "InstallLocation"))
                    if p:
                        return p
                finally:
                    k.Close()
        finally:
            base.Close()
    return None


def _exe_from_start_menu():
    try:
        import win32com.client
        sh = win32com.client.Dispatch("WScript.Shell")
    except Exception as exc:
        dev_log("开始菜单 .lnk 解析初始化失败", exc)
        return None
    menus = [
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    for m in menus:
        if not m or not os.path.isdir(m):
            continue
        for dirpath, _dirs, files in os.walk(m):
            for fn in files:
                if fn.lower().endswith(".lnk") and GAME_TITLE_KEY in fn:
                    try:
                        t = sh.CreateShortcut(os.path.join(dirpath, fn)).TargetPath
                        if t and os.path.isfile(t):
                            return t
                    except Exception:
                        continue
    return None


def _remember_game_path(p):
    '把定位到的 exe 路径缓存到 config.gamepath —— 以后启动直接读、免再搜索(失效会自动重搜更新)。'
    try:
        from config import cfg
        if cfg.get("game_path") != p:
            cfg.set("game_path", p)
            dev_log(f"已记录启动器路径到 game_path:{p}")
    except Exception as exc:
        dev_log("保存 game_path 失败", exc)


def find_game_exe():
    '定位《王者荣耀世界》启动器 exe(跨机器)。'
    p = _exe_from_config()
    if p:
        return p
    for fn in (_exe_from_registry, _exe_from_start_menu):
        try:
            p = fn()
        except Exception as exc:
            dev_log(f"find_game_exe.{fn.__name__} 失败", exc)
            p = None
        if p:
            _remember_game_path(p)
            return p
    return None


def _is_game_title(title) -> bool:
    '是否游戏/启动器窗口标题(精确 = 「王者荣耀世界」,去首尾空白)。'
    return bool(title) and title.strip() == GAME_TITLE_KEY


def game_windows():
    '枚举全部启动器/游戏窗口，并按前台、非最小化、有效面积排序。'
    found = []
    foreground = win32gui.GetForegroundWindow()

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and _is_game_title(win32gui.GetWindowText(h)):
            try:
                left, top, right, bottom = win32gui.GetClientRect(h)
                area = max(0, right - left) * max(0, bottom - top)
                iconic = bool(win32gui.IsIconic(h))
            except Exception:
                area, iconic = 0, True
            found.append((h, h == foreground, not iconic, area))

    win32gui.EnumWindows(_cb, None)
    found.sort(key=lambda item: (item[1], item[2], item[3]), reverse=True)
    return [item[0] for item in found]


def any_game_window():
    '返回当前最适合识别的启动器/游戏窗口，而不是 EnumWindows 遇到的第一个。'
    windows = game_windows()
    return windows[0] if windows else 0








GA_ROOT = 2   


def _win_pid(h) -> int:
    try:
        import win32process
        return win32process.GetWindowThreadProcessId(h)[1]
    except Exception:
        return 0


def _top_window_at(sx, sy):
    '屏幕点 (sx,sy) 处视觉最前的顶层窗口(WindowFromPoint → GAROOT 祖先);失败返回 0。'
    try:
        w = win32gui.WindowFromPoint((int(sx), int(sy)))
        if not w:
            return 0
        root = ctypes.windll.user32.GetAncestor(w, GA_ROOT)
        return root or w
    except Exception:
        return 0


def _same_process_on_top(h, sx, sy) -> bool:
    '屏幕点 (sx,sy) 处视觉最前的窗口是否属于 h 的进程(CEF 子窗口标题不精确 → 按 pid 判)。'
    top = _top_window_at(sx, sy)
    if not top:
        return False
    if top == h:
        return True
    p_top, p_h = _win_pid(top), _win_pid(h)
    return bool(p_top) and p_top == p_h








WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
MK_LBUTTON = 0x0001
_CWP_SKIP = 0x0001 | 0x0002 | 0x0004        


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_u32 = ctypes.windll.user32
_u32.ChildWindowFromPointEx.restype = ctypes.c_void_p
_u32.ChildWindowFromPointEx.argtypes = [ctypes.c_void_p, _POINT, ctypes.c_uint]
_u32.SendMessageTimeoutW.restype = ctypes.c_void_p
_u32.SendMessageTimeoutW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
]

SMTO_ABORTIFHUNG = 0x0002


def _send_message_bounded(hwnd, msg, wparam, lparam, timeout_ms=250) -> bool:
    '同步投递一条窗口消息，但目标线程卡死时最多等待 timeoutms。'
    result = ctypes.c_size_t()
    try:
        ok = _u32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd), int(msg), ctypes.c_size_t(wparam),
            ctypes.c_ssize_t(lparam), SMTO_ABORTIFHUNG, int(timeout_ms),
            ctypes.byref(result))
        return bool(ok)
    except Exception as exc:
        dev_log("SendMessageTimeout 后台输入失败", exc)
        return False


def _send_or_post(hwnd, msg, wparam, lparam) -> bool:
    '优先确认目标已处理消息；超时后退回异步投递，绝不切换前台。'
    if _send_message_bounded(hwnd, msg, wparam, lparam):
        return True
    try:
        win32gui.PostMessage(hwnd, msg, wparam, lparam)
        return True
    except Exception as exc:
        dev_log("PostMessage 后台输入失败", exc)
        return False


def _deep_child_at(hwnd, cx, cy):
    '从顶层窗口下钻到客户区点 (cx,cy) 处最深的可见子窗口,返回 (子窗 hwnd, 子窗客户区坐标)。'
    cur, pt = hwnd, (int(cx), int(cy))
    for _ in range(16):
        try:
            ch = _u32.ChildWindowFromPointEx(cur, _POINT(pt[0], pt[1]), _CWP_SKIP)
        except Exception:
            break
        if not ch or ch == cur:
            break
        sx, sy = win32gui.ClientToScreen(cur, pt)
        pt = win32gui.ScreenToClient(ch, (sx, sy))
        cur = ch
    return cur, pt


def _post_click_client(top_hwnd, cx, cy) -> bool:
    '后台点击:向最深子窗口同步限时投递 MOVE/DOWN/UP，失败才异步投递。'
    try:
        tgt, (tx, ty) = _deep_child_at(top_hwnd, cx, cy)
        lp = ((ty & 0xFFFF) << 16) | (tx & 0xFFFF)
        if not _send_or_post(tgt, WM_MOUSEMOVE, 0, lp):
            return False
        time.sleep(0.02)
        if not _send_or_post(tgt, WM_LBUTTONDOWN, MK_LBUTTON, lp):
            return False
        time.sleep(0.03)
        return _send_or_post(tgt, WM_LBUTTONUP, 0, lp)
    except Exception as exc:
        dev_log("后台消息点击失败", exc)
        return False


def _post_key(top_hwnd, vk) -> bool:
    '后台按键:PostMessage WMKEYDOWN/KEYUP(同类脚本 keyboard=PostMessage 同款)。'
    try:
        l, t, r, b = win32gui.GetClientRect(top_hwnd)
        tgt, _ = _deep_child_at(top_hwnd, (r - l) // 2, (b - t) // 2)
        win32gui.PostMessage(tgt, WM_KEYDOWN, vk, 0x00000001)
        time.sleep(0.03)
        win32gui.PostMessage(tgt, WM_KEYUP, vk, 0xC0000001)
        return True
    except Exception as exc:
        dev_log("后台消息按键失败", exc)
        return False


def _restore_no_activate(hwnd) -> bool:
    '恢复最小化窗口但不激活、不置顶，供 PrintWindow 继续后台识别。'
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        
        _u32.ShowWindowAsync(ctypes.c_void_p(hwnd), 4)
        _u32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010 | 0x0040)
        return not bool(win32gui.IsIconic(hwnd))
    except Exception as exc:
        dev_log("后台恢复启动器失败", exc)
        return False


def _print_window_bgr(hwnd):
    '后台截图:PrintWindow(PWCLIENTONLY|PWRENDERFULLCONTENT)取窗口自身客户区画面。'
    dc = mfc = mem = bmp = None
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
        dc = win32gui.GetDC(hwnd)
        mfc = win32ui.CreateDCFromHandle(dc)
        mem = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, w, h)
        mem.SelectObject(bmp)
        ok = _u32.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(mem.GetSafeHdc()), 3)  
        if not ok:
            return None
        bits = bmp.GetBitmapBits(True)
        return np.ascontiguousarray(np.frombuffer(bits, np.uint8).reshape(h, w, 4)[:, :, :3])
    except Exception as exc:
        dev_log("PrintWindow 后台截图失败", exc)
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            if mem is not None:
                mem.DeleteDC()
        except Exception:
            pass
        try:
            if mfc is not None:
                mfc.DeleteDC()
        except Exception:
            pass
        try:
            if dc is not None:
                win32gui.ReleaseDC(hwnd, dc)
        except Exception:
            pass


def launch_game_exe(path):
    '直接创建启动器进程；创建失败立即抛错，不做延迟补拉。'
    return subprocess.Popen(
        [path], cwd=os.path.dirname(path) or None, close_fds=True)


def _norm1920(frame):
    '宽 > NORMW 时按比例降采样到 NORMW(ROI/点击都用归一化分数,缩放不影响坐标),省 OCR 时间。'
    h, w = frame.shape[:2]
    if w <= NORM_W:
        return frame
    nh = max(1, int(round(h * NORM_W / w)))
    return cv2.resize(frame, (NORM_W, nh), interpolation=cv2.INTER_AREA)


def _crop(frame, roi):
    '按归一化 roi=(x0,y0,x1,y1) 切子图。'
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = roi
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


class GameLauncher:
    TICK = 0.4                 
    MIN_CONF = 0.5             
    CLICK_DELAY = (0.2, 0.5)   
    WORLD_HUD_CONFIRM = 2      
    MONTHLY_CONFIRM = 2        
    START_STABLE_S = 2.0       
    TOTAL_TIMEOUT_S = 300.0    
    LAUNCH_WAIT_S = 90.0       
    CLOSE_GRACE_S = 6.0        
    AFTER_CLICK_S = 1.0        

    
    ROI_LAUNCH_BTN = (0.74, 0.82, 0.97, 0.96)   
    ROI_ANNOUNCE = (0.10, 0.15, 0.38, 0.32)     
    ROI_START = (0.30, 0.68, 0.70, 0.86)        
    ROI_WORLD_HUD_RIGHT = (0.70, 0.02, 1.00, 0.96)  
    ROI_WORLD_HUD_BOTTOM = (0.00, 0.82, 0.30, 1.00) 
    LAUNCH_BTN_PT = (0.867, 0.892)
    CLOSE_X_PT = (0.808, 0.258)
    START_GAME_PT = (0.482, 0.820)
    VK_ESC = 0x1B                               
    
    LOADING_KEYS = ("初始化", "检测版本", "检测资源", "在进入", "正在进入", "进入游戏",
                    "着色", "编译", "Unreal", "Epic", "天美", "TiMi", "加载中", "请稍候", "图形")
    WORLD_HUD_KEYS = ("抓拍", "好友", "切换", "输入", "高处")

    def __init__(self, log=print, on_count=lambda n: None,
                 input_tick_at_start: int | None = None) -> None:
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.success = False
        self._clicked_launch = False   
        self._launch_cd = 0.0          
        self._saw_window = False   
        self._driving_launcher = False  
        self._announce_tries = 0   
        self._start_since = 0.0    
        self._last_diag = 0.0      
        self._launcher_hwnd = 0    
        self._game_win_seen = False  
        self._exit_ready = 0       
        self._start_clicked = 0    
        self._hwnd = 0
        self._restored_hwnds = set()
        self._input_tick_at_start = input_tick_at_start
        self._foreground_handoff_attempted = False
        self._foreground_handoff_done = False
        self._foreground_wait_logged = False

    
    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        if self.paused:
            release_known_keys(self.log)

    def _stopped(self) -> bool:
        return bool(self.stop_flag or self.paused)

    def _foreground(self) -> bool:
        '点击守卫(方案 B):允许点击的条件——。'
        h = self._hwnd
        if not h:
            return False
        fg = win32gui.GetForegroundWindow()
        if fg == h:
            return True
        if fg and _win_pid(fg) and _win_pid(fg) == _win_pid(h):   
            return True
        if self._fg_owner_kind(fg) == "other":                    
            return False
        return self._visible_state(h) == "top"

    def _prepare_game_ui_input(self, reason: str) -> bool:
        '在游戏内界面首次需要输入时完成唯一一次前台交接。'
        h = int(self._hwnd or 0)
        if h and is_foreground(h):
            self._foreground_handoff_attempted = True
            self._foreground_handoff_done = True
            self._foreground_wait_logged = False
            return True
        if self._foreground_handoff_attempted:
            if not self._foreground_wait_logged:
                self.log(f"游戏不在前台，暂停{reason}操作；切回游戏后自动继续")
                self._foreground_wait_logged = True
            return False
        self._foreground_handoff_attempted = True
        if not can_auto_activate_game(self._input_tick_at_start):
            self.log(f"检测到用户正在操作其他程序，未切换游戏前台；暂停{reason}操作")
            self._foreground_wait_logged = True
            return False
        activated = bool(activate_game_window(h) and is_foreground(h))
        self._foreground_handoff_done = activated
        if activated:
            self.log(f"已将游戏切到前台一次，开始{reason}操作")
            time.sleep(0.25)
            return True
        self.log(f"游戏前台切换失败，本轮不再重试；切回游戏后自动继续{reason}操作")
        self._foreground_wait_logged = True
        return False

    def _fg_owner_kind(self, fg) -> str:
        "前台窗口归谁:'self'=本程序 / 'shell'=桌面·任务栏 / 'other'=第三方程序(用户在用,别打扰)。"
        if not fg:
            return "shell"
        pid = _win_pid(fg)
        if pid and pid == os.getpid():
            return "self"
        try:
            if win32gui.GetClassName(fg) in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return "shell"
        except Exception:
            pass
        return "other"

    def _probe_screen_pts(self, h):
        'h 客户区上要核验"视觉最前"的屏幕采样点:客户区中心 + 两个主要点击点(启动游戏/开始游戏)。'
        try:
            from winenv import client_rect_on_screen
            x, y, w, hh = client_rect_on_screen(h)
        except Exception as exc:
            dev_log("取客户区屏幕矩形失败", exc)
            return []
        if w <= 0 or hh <= 0:
            return []
        return [(int(x + px * w), int(y + py * hh))
                for px, py in ((0.5, 0.5), self.LAUNCH_BTN_PT, self.START_GAME_PT)]

    def _visible_state(self, h) -> str:
        "h 是否视觉最前:'top' 全部采样点都是它 / 'coveredself' 被本程序 GUI 挡住(可自己让开)/。"
        pts = self._probe_screen_pts(h)
        if not pts:
            return "covered_other"
        me = os.getpid()
        for sx, sy in pts:
            if _same_process_on_top(h, sx, sy):
                continue
            top = _top_window_at(sx, sy)
            if top and _win_pid(top) == me:
                return "covered_self"
            return "covered_other"
        return "top"

    def _resolve_window(self):
        '定位窗口并返回 (状态, hwnd)——后台模式(同类脚本 同款):。'
        fg = win32gui.GetForegroundWindow()
        if fg and _is_game_title(win32gui.GetWindowText(fg)):   
            self._hwnd = fg
            return "act", fg
        h = any_game_window()
        if not h:
            return "gone", 0
        if win32gui.IsIconic(h):
            
            if h not in self._restored_hwnds:
                self._restored_hwnds.add(h)
                self.log("检测到启动器/游戏最小化 → 后台无激活恢复一次")
            if not _restore_no_activate(h):
                return "min", h
        self._hwnd = h
        return "act", h

    def _click(self, pt) -> bool:
        '仅用后台窗口消息点击，不移动鼠标、不激活窗口、不申请前台。'
        if self._stopped() or not input_allowed():
            return False
        time.sleep(random.uniform(*self.CLICK_DELAY))
        if self._stopped() or not input_allowed():
            return False
        h = self._hwnd
        try:
            l, t, r, b = win32gui.GetClientRect(h)
            cw, ch = r - l, b - t
        except Exception as exc:
            dev_log("取客户区失败(窗口可能刚关闭)", exc)
            return False
        if cw <= 0 or ch <= 0:
            return False
        if self._stopped() or not input_allowed():
            return False
        return _post_click_client(h, int(pt[0] * cw), int(pt[1] * ch))

    def _click_game_ui(self, pt) -> bool:
        '仅在游戏已经位于前台时发送真实鼠标点击。'
        if not self._prepare_game_ui_input("游戏界面"):
            return False
        return safe_click_norm(
            self._hwnd, pt, self._stopped, self._foreground, self.log, 0.03)

    def _press_esc_bg(self) -> bool:
        '后台 ESC(PostMessage,不需要前台)。'
        if self._stopped() or not input_allowed():
            return False
        return _post_key(self._hwnd, self.VK_ESC)

    def _press_esc(self) -> bool:
        return safe_press_key(
            self.VK_ESC, self._stopped, self._foreground, self.log, 0.05)

    
    def _ocr_join(self, f, roi) -> str:
        sub = _crop(f, roi)
        if sub is None or sub.size == 0:
            return ""
        try:
            res, _ = _get_ocr()(sub)
        except Exception as exc:
            dev_log("启动器 OCR 失败", exc)
            return ""
        parts = []
        for it in (res or []):
            try:
                if float(it[2]) >= self.MIN_CONF:
                    parts.append(str(it[1]))
            except (TypeError, ValueError, IndexError):
                try:
                    parts.append(str(it[1]))
                except Exception:
                    pass
        return "".join(parts)

    def _ocr_lines(self, f, roi):
        'OCR 该 ROI → [(text, cxnorm, cynorm), ...](文字框中心按客户区归一化)。'
        sub = _crop(f, roi)
        if sub is None or sub.size == 0:
            return []
        try:
            res, _ = _get_ocr()(sub)
        except Exception as exc:
            dev_log("启动器 OCR 失败", exc)
            return []
        H, W = f.shape[:2]
        ox, oy = roi[0] * W, roi[1] * H
        out = []
        for it in (res or []):
            try:
                box, txt, score = it[0], str(it[1]).strip(), float(it[2])
            except (IndexError, ValueError, TypeError):
                continue
            if not txt or score < self.MIN_CONF:
                continue
            cx = (ox + sum(p[0] for p in box) / len(box)) / W
            cy = (oy + sum(p[1] for p in box) / len(box)) / H
            out.append((txt, cx, cy))
        return out

    def read_launch_button(self, f):
        '启动器右下角按钮 → (state, 点击点):ready(启动游戏)/ exiting / ingame / none。'
        lines = self._ocr_lines(f, self.ROI_LAUNCH_BTN)
        t = "".join(ln[0] for ln in lines)
        if "启动" in t:                       
            pt = next(((cx, cy) for txt, cx, cy in lines if "启动" in txt), None)
            return "ready", (pt or self.LAUNCH_BTN_PT)
        if "退出" in t:                       
            return "exiting", None
        if "游戏中" in t or ("游戏" in t and "中" in t):
            return "ingame", None
        return "none", None

    def has_announcement(self, f) -> bool:
        return "公告" in self._ocr_join(f, self.ROI_ANNOUNCE)

    def find_start_game(self, f):
        '正中偏下「开始游戏」→ 文字框中心(归一化);没有则 None。'
        lines = self._ocr_lines(f, self.ROI_START)
        for txt, cx, cy in lines:
            if "开始游戏" in txt:
                return (cx, cy)
        joined = "".join(ln[0] for ln in lines)   
        if "开始游戏" in joined:
            return next(((cx, cy) for txt, cx, cy in lines if "开始" in txt or "游戏" in txt), self.START_GAME_PT)
        return None

    def read_world_hud(self, f) -> tuple[bool, tuple[str, ...], str]:
        '正向识别角色已经可操作的游戏画面，不读取历史阶段标记。'
        text = (self._ocr_join(f, self.ROI_WORLD_HUD_RIGHT)
                + self._ocr_join(f, self.ROI_WORLD_HUD_BOTTOM))
        hits = tuple(key for key in self.WORLD_HUD_KEYS if key in text)
        return len(hits) >= 2, hits, text

    def read_monthly_card(self, f) -> tuple[bool, str, str]:
        '识别遮住角色 HUD 的月卡浮层，只作为“已进入游戏”的正向证据。'
        return read_monthly_card(f)

    def _ensure_launcher_running(self) -> bool:
        '没有游戏/启动器窗口时:本地定位 exe → 拉起 → 等启动器窗口出现。'
        exe = find_game_exe()
        if not exe:
            self.log("未找到《王者荣耀世界》本地安装路径;请手动打开启动器,或在 data/config.json 的 game_path 指定 exe")
            return False
        self.log(f"未检测到游戏窗口 → 本地启动:{exe}")
        self._driving_launcher = True       
        try:
            launch_game_exe(exe)
        except Exception as exc:
            dev_log("拉起游戏 exe 失败", exc)
            self.log(f"启动失败:{type(exc).__name__}: {exc}")
            return False
        deadline = time.time() + self.LAUNCH_WAIT_S
        while not self.stop_flag and time.time() < deadline:
            if self.paused:
                paused_at = time.monotonic()
                while self.paused and not self.stop_flag:
                    time.sleep(0.2)
                deadline += time.monotonic() - paused_at
                continue
            if any_game_window():
                self.log("启动器已出现,开始识别")
                time.sleep(1.0)
                return True
            time.sleep(0.5)
        if not self.stop_flag:
            self.log("等待启动器窗口出现超时,已停止")
        return False

    
    def run(self) -> bool:
        '每帧从当前画面重新判定阶段并分派动作。'
        
        if self.stop_flag:
            return False
        
        while self.paused and not self.stop_flag:
            time.sleep(0.2)
        if self.stop_flag:
            return False
        self.success = False
        self._clicked_launch = False
        self._launch_cd = 0.0
        self._driving_launcher = False
        self._announce_tries = 0
        self._start_since = 0.0
        self._launcher_hwnd = 0
        self._game_win_seen = False
        self._exit_ready = 0
        self._start_clicked = 0
        self._restored_hwnds.clear()
        self.log("自动启动游戏:开始(后台识别+后台点击:不需要前台、不动你的鼠标,"
                 "你可随意看日志/用别的程序;只跑一轮,完成即停;F12 急停)")
        
        if not any_game_window() and not self._ensure_launcher_running():
            return self.success
        world_hud_count = 0
        monthly_count = 0
        deadline = time.time() + self.TOTAL_TIMEOUT_S
        last = ""
        grace = None
        self._saw_window = False
        try:
            with GameCapture(0) as cap:        
                while not self.stop_flag and time.time() < deadline:
                    if self.paused:
                        paused_at = time.monotonic()
                        while self.paused and not self.stop_flag:
                            time.sleep(0.2)
                        deadline += time.monotonic() - paused_at
                        continue
                    state, gw = self._resolve_window()
                    if state == "gone":
                        
                        
                        
                        
                        
                        
                        if self._clicked_launch:
                            if self._game_win_seen:
                                if grace is None:
                                    grace = time.time()
                                if time.time() - grace > self.CLOSE_GRACE_S:
                                    self.log("游戏窗口已消失(你在启动过程中退出了游戏)→ 自动启动结束")
                                    return self.success
                            elif last != "transition":
                                self.log("启动器已关、游戏加载中…等待游戏窗口(着色器编译可能较久)")
                                last = "transition"
                        elif self._saw_window:
                            if grace is None:
                                grace = time.time()
                            if time.time() - grace > self.CLOSE_GRACE_S:
                                self.log("启动器已被关闭,停止运行")
                                return self.success
                        elif last != "wait_win":
                            self.log("等待《王者荣耀世界》窗口…")
                            last = "wait_win"
                        time.sleep(self.TICK)
                        continue
                    grace = None
                    self._saw_window = True
                    if self._clicked_launch and self._launcher_hwnd and gw and gw != self._launcher_hwnd:
                        self._game_win_seen = True   
                    if state == "min":
                        
                        if last != "min":
                            self.log("启动器/游戏已最小化 → 暂停(恢复后自动继续)")
                            last = "min"
                        time.sleep(0.3)
                        continue
                    
                    
                    f = _print_window_bgr(gw)
                    if f is None and self._visible_state(gw) == "top":
                        cap.hwnd = gw
                        f = cap.grab()
                    if f is None:
                        time.sleep(self.TICK)
                        continue
                    fn = _norm1920(f)

                    
                    
                    
                    btn, bpt = self.read_launch_button(fn)
                    if btn in ("ready", "exiting", "ingame"):
                        self._driving_launcher = True
                        world_hud_count = 0
                        monthly_count = 0
                    if btn != "ready":
                        self._exit_ready = 0               

                    if btn == "ready":
                        
                        
                        if self._clicked_launch and self._game_win_seen:
                            self._exit_ready += 1
                            if self._exit_ready >= 2:
                                self.log("检测到游戏已退出、回到启动器 → 停止,不再重新启动")
                                return self.success
                            time.sleep(self.TICK)
                            continue
                        nowt = time.time()
                        if nowt >= self._launch_cd:        
                            self.log("启动器就绪 → 点击「启动游戏」")
                            self._launcher_hwnd = self._hwnd   
                            if self._click(bpt):
                                self._clicked_launch = True
                                self._launch_cd = nowt + 5.0
                                last = ""
                        elif last != "launching":
                            self.log("已点「启动游戏」,等待进入…")
                            last = "launching"
                        time.sleep(self.TICK)
                        continue
                    if btn == "exiting":
                        if last != "exiting":
                            self.log("启动器「退出中」→ 等待上一局退出后再启动…")
                            last = "exiting"
                        time.sleep(self.TICK)
                        continue
                    if btn == "ingame":
                        self._launcher_hwnd = self._hwnd   
                        self.log("启动器显示「游戏中」→ 游戏已在运行,无需启动,直接结束")
                        self.success = True
                        return True

                    
                    
                    ann_txt = self._ocr_join(fn, self.ROI_ANNOUNCE)
                    start_lines = self._ocr_lines(fn, self.ROI_START)
                    if "公告" in ann_txt:
                        world_hud_count = 0
                        monthly_count = 0
                        self._driving_launcher = True       
                        self._start_since = 0.0             
                        attempt = self._announce_tries + 1
                        if not self._prepare_game_ui_input("公告"):
                            time.sleep(self.TICK)
                            continue
                        if attempt % 2 == 1:
                            self.log("检测到「公告」→ 点击右上角关闭按钮")
                            sent = self._click_game_ui(self.CLOSE_X_PT)
                        else:
                            self.log("公告仍在 → 按 ESC 关闭")
                            sent = self._press_esc()
                        if sent:
                            self._announce_tries = attempt
                        time.sleep(0.8)
                        last = ""
                        continue
                    self._announce_tries = 0
                    sp = next(((cx, cy) for txt, cx, cy in start_lines if "开始游戏" in txt), None)
                    if sp:
                        world_hud_count = 0
                        monthly_count = 0
                        
                        
                        if self._start_since == 0.0:
                            self._start_since = time.time()
                        if (time.time() - self._start_since >= self.START_STABLE_S
                                and time.time() >= self._launch_cd):
                            attempt = self._start_clicked + 1
                            if not self._prepare_game_ui_input("开始游戏"):
                                time.sleep(self.TICK)
                                continue
                            self.log(f"检测到「开始游戏」→ 点击进入游戏(第 {attempt} 次)")
                            if self._click_game_ui(sp):
                                self._start_clicked = attempt
                                self._launch_cd = time.time() + 2.0   
                                time.sleep(self.AFTER_CLICK_S)
                        elif last != "start_confirm":
                            self.log("检测到「开始游戏」,稳定确认中…(避开公告前的瞬时闪现)")
                            last = "start_confirm"
                        time.sleep(self.TICK)
                        continue
                    self._start_since = 0.0                 
                    monthly, monthly_title, monthly_reward = self.read_monthly_card(fn)
                    if monthly:
                        monthly_count += 1
                        world_hud_count = 0
                        self._driving_launcher = True
                        if monthly_count >= self.MONTHLY_CONFIRM:
                            self.on_count(1)
                            self.success = True
                            self.log("检测到登录奖励，确认已进入游戏")
                            return True
                        if last != "monthly_confirm":
                            self.log("检测到登录奖励，确认中…")
                            last = "monthly_confirm"
                        time.sleep(self.TICK)
                        continue
                    monthly_count = 0
                    in_world, hud_hits, hud_text = self.read_world_hud(fn)
                    if in_world:
                        world_hud_count += 1
                        if world_hud_count >= self.WORLD_HUD_CONFIRM:
                            self.on_count(1)
                            self.success = True
                            self.log(f"识别到角色 HUD {hud_hits}，进入游戏完成")
                            return True
                        if last != "world_confirm":
                            self.log(f"识别到角色 HUD {hud_hits}，稳定确认中…")
                            last = "world_confirm"
                        time.sleep(self.TICK)
                        continue
                    world_hud_count = 0
                    if self._start_clicked:
                        
                        self._driving_launcher = True

                    
                    
                    
                    start_txt = "".join(ln[0] for ln in start_lines)
                    loading = any(k in (ann_txt + start_txt) for k in self.LOADING_KEYS)
                    if self._clicked_launch or self._driving_launcher or loading:
                        if loading:
                            self._driving_launcher = True   
                            if last != "loading":
                                self.log("当前画面识别为加载/着色器，不动作…")
                                last = "loading"
                        elif self._start_clicked:
                            if last != "wait_world":
                                self.log("「开始游戏」已消失，当前帧未命中角色 HUD，继续重新识别…")
                                last = "wait_world"
                        elif last != "unknown_progress":
                            self.log("启动流程中的当前界面未命中已知特征，继续重新识别…")
                            last = "unknown_progress"
                        nowt = time.time()                  
                        if nowt - self._last_diag > 3.0:
                            self._last_diag = nowt
                            dev_log(f"[launcher diag] 开始游戏ROI={start_txt!r} "
                                    f"公告ROI={ann_txt!r} 角色HUD={hud_text!r} "
                                    f"月卡OCR=({monthly_title!r}, {monthly_reward!r})")
                        time.sleep(self.TICK)
                        continue
                    
                    if last != "unknown":
                        self.log("当前界面未确认，继续等待…")
                        last = "unknown"
                    time.sleep(self.TICK)
        finally:
            release_known_keys(self.log)
        if not self.stop_flag:
            self.log("自动启动游戏:超时结束(界面与预期不同,或加载过久)")
        return self.success


if __name__ == "__main__":
    GameLauncher().run()
