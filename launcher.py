"""launcher.py — 自动启动游戏(启动器 → 进入游戏),对齐 MaaNTE/okww 的"识别→点击"流水。

**为什么不能像剧情/采集那样绑定单一窗口**:启动器与游戏窗口**同标题**「王者荣耀世界」但 **hwnd 不同**
(录制实测 920228=启动器 → 4850284=游戏);点「启动游戏」后窗口会更换。故本模块每帧**跟随当前前台窗口**
(winenv.foreground_target),只要标题含「王者荣耀世界」就锁定它来截图/点击。

**不必手动开启动器**:run() 开头若没有任何《王者荣耀世界》窗口,就**本地定位启动器 exe 并拉起**,等启动器窗口出现再进识别。
定位跨机器通用(`find_game_exe`):① 配置 `game_path` → ② 注册表卸载项 DisplayIcon/InstallLocation → ③ 开始菜单 .lnk。
(KingLauncher 下是 `<版本号>\王者荣耀世界.exe`,版本目录随更新变,故优先 DisplayIcon/.lnk,InstallLocation 兜底取最新版本子目录。)

**按"当前界面状态"分派的单循环**(只跑一轮,完成即停,不重复启动;只读屏 + 标准点击;仅前台时动作;可停 / F12 急停):
  每帧判当前是哪种界面,按优先级做对应动作 ——
  · 「开始游戏」在 → 点它进入游戏 → **完成(成功)**。
  · 「公告」弹窗在 → 点右上角 X 关闭。
  · 启动器右下角按钮:读到「启动游戏」→ 点它;「退出中」(刚退上一局)→ 等;「游戏中」→ 当作已启动,等公告/开始游戏。
  · 以上都不是、且本轮**没点过启动** → 当前就是普通游戏内(非启动流程="非剧情")状态 → **判「已在游戏中」,不启动,直接结束**。
  这样:用户在游戏游玩界面重复点启动 → 不会再启动,只是直接结束(可接实时检测);点过启动后加载(初始化/检测版本/加载/着色器)不动作。

**关键点击坐标=录制真值**(sessions/20260628_113815/events.jsonl,归一化到各自窗口客户区):
  启动游戏≈(0.867, 0.892)、公告关闭X≈(0.808, 0.258)、开始游戏≈(0.482, 0.820)。
状态用小 ROI 内 OCR 文字**判定有无**,命中后点上面的定值坐标(归一化,跨分辨率稳定)。
"""
from __future__ import annotations

import ctypes
import os
import random
import time
import winreg

import cv2
import numpy as np
import win32con
import win32gui
import win32ui

from capture import GameCapture
from fishing.matcher import _get_ocr
from runtime_guard import dev_log, release_known_keys, safe_click_norm, safe_press_key

NORM_W = 1920                       # 识别基准宽(同 fishing/template_bank.NORM_W);大于此先降采样,限 OCR 开销
GAME_TITLE_KEY = "王者荣耀世界"      # 启动器与游戏窗口标题都含它(可能带尾空格)
GAME_EXE_NAME = "王者荣耀世界.exe"   # 启动器可执行文件名(KingLauncher\<版本>\ 下)


# ----------------------------- 本地定位 + 拉起启动器 -----------------------------
# 目标:不打开启动器也能自动启动,且**跨机器/跨安装位置**通用。
# 来源优先级:① 用户配置 game_path → ② 注册表卸载项 DisplayIcon/InstallLocation → ③ 开始菜单快捷方式。
# 注意:KingLauncher 下是 `<版本号>\王者荣耀世界.exe`,版本目录随更新变 → InstallLocation 可能指向旧版本,
# 故优先用 DisplayIcon / 开始菜单 .lnk(更新器会同步成当前版本),InstallLocation 兜底时取"最新版本子目录"。

def _safe_listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _find_exe_under(root):
    """在 root 及其(版本号)子目录、以及同级目录里找 王者荣耀世界.exe,取最新修改的(应对旧版本目录残留)。"""
    if not root:
        return None
    root = str(root).strip().strip('"').rstrip("\\/")
    dirs = []
    if os.path.isdir(root):
        dirs.append(root)
        dirs += [os.path.join(root, s) for s in _safe_listdir(root)]       # <ver>\exe
        parent = os.path.dirname(root)
        if os.path.isdir(parent):
            dirs += [os.path.join(parent, s) for s in _safe_listdir(parent)]  # 同级更新版本
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
    """遍历卸载项,DisplayName 含「王者荣耀世界」→ DisplayIcon(去图标索引)优先,其次 InstallLocation 找 exe。"""
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
    """把定位到的 exe 路径缓存到 config.game_path —— 以后启动直接读、免再搜索(失效会自动重搜更新)。"""
    try:
        from config import cfg
        if cfg.get("game_path") != p:
            cfg.set("game_path", p)
            dev_log(f"已记录启动器路径到 game_path:{p}")
    except Exception as exc:
        dev_log("保存 game_path 失败", exc)


def find_game_exe():
    """定位《王者荣耀世界》启动器 exe(跨机器)。
    **先读本地缓存 `game_path`**(存在即用,免搜索);否则查注册表 / 开始菜单,**找到后写回缓存**。
    缓存路径失效(游戏更新换了版本目录、文件已不在)→ `_exe_from_config` 返回 None → 自动重搜并更新缓存。"""
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
    """是否游戏/启动器窗口标题(**精确** = 「王者荣耀世界」,去首尾空白)。
    必须精确:**否则会匹配到本程序自己的窗口**「HOKWorld … · 王者荣耀世界」,把自身当成游戏窗口
    → 永远以为游戏已开(不拉起 exe)、还会截/点本程序窗口。"""
    return bool(title) and title.strip() == GAME_TITLE_KEY


def any_game_window():
    """是否存在《王者荣耀世界》启动器/游戏窗口(精确标题,排除本程序窗口)。返回 hwnd 或 0。"""
    found = []

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and _is_game_title(win32gui.GetWindowText(h)):
            found.append(h)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


# ----------------------------- 视觉最前判定(方案 B 的核心) -----------------------------
# 本工具的截图(capture.GameCapture)是**从桌面 DC 按屏幕矩形截**,点击是**硬件光标点在屏幕坐标上**——
# 两者作用的都是"屏幕上视觉最前的内容",与键盘焦点(GetForegroundWindow)无关。
# 所以判"能否读屏/点击"的正确判据是:**启动器/游戏窗口在目标点上视觉最前**(WindowFromPoint)。
# KingLauncher 是 CEF 多窗口程序:命中的常是它标题不精确的子窗口 → 一律**按进程号**归属判断。

GA_ROOT = 2   # GetAncestor:取顶层根窗口


def _win_pid(h) -> int:
    try:
        import win32process
        return win32process.GetWindowThreadProcessId(h)[1]
    except Exception:
        return 0


def _top_window_at(sx, sy):
    """屏幕点 (sx,sy) 处视觉最前的**顶层**窗口(WindowFromPoint → GA_ROOT 祖先);失败返回 0。"""
    try:
        w = win32gui.WindowFromPoint((int(sx), int(sy)))
        if not w:
            return 0
        root = ctypes.windll.user32.GetAncestor(w, GA_ROOT)
        return root or w
    except Exception:
        return 0


def _same_process_on_top(h, sx, sy) -> bool:
    """屏幕点 (sx,sy) 处视觉最前的窗口是否属于 h 的进程(CEF 子窗口标题不精确 → 按 pid 判)。"""
    top = _top_window_at(sx, sy)
    if not top:
        return False
    if top == h:
        return True
    p_top, p_h = _win_pid(top), _win_pid(h)
    return bool(p_top) and p_top == p_h


# ----------------------------- 后台输入 / 后台截图(MaaNTE 同款机制) -----------------------------
# MaaNTE(assets/interface.json)默认控制器:screencap="Background"、mouse="SendMessageWithCursorPos"、
# keyboard="PostMessage" —— 鼠标/键盘做成**窗口消息直接发给目标 hwnd**、画面用**后台方式取窗口自身内容**。
# 好处:**完全不需要前台/焦点、不动真实鼠标、不怕被遮挡** → 用户可随意点本程序看日志、用别的程序,互不干扰。
# 这也是"自动点启动器"屡试屡败的真正解:此前纠结"怎么把启动器弄成前台",方向本身就错了。

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
MK_LBUTTON = 0x0001
_CWP_SKIP = 0x0001 | 0x0002 | 0x0004        # SKIPINVISIBLE | SKIPDISABLED | SKIPTRANSPARENT


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_u32 = ctypes.windll.user32
_u32.ChildWindowFromPointEx.restype = ctypes.c_void_p
_u32.ChildWindowFromPointEx.argtypes = [ctypes.c_void_p, _POINT, ctypes.c_uint]


def _deep_child_at(hwnd, cx, cy):
    """从顶层窗口下钻到客户区点 (cx,cy) 处**最深的可见子窗口**,返回 (子窗 hwnd, 子窗客户区坐标)。
    CEF(KingLauncher)/UE 的鼠标消息要发给真正承载渲染的子窗口才会被处理,发给顶层壳常被忽略。"""
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
    """后台点击:向 (cx,cy)(top_hwnd 客户区坐标)处的最深子窗口 PostMessage
    WM_MOUSEMOVE→LBUTTONDOWN→LBUTTONUP。不动真实鼠标、不需要前台(MaaNTE mouse=SendMessage 同款)。"""
    try:
        tgt, (tx, ty) = _deep_child_at(top_hwnd, cx, cy)
        lp = ((ty & 0xFFFF) << 16) | (tx & 0xFFFF)
        win32gui.PostMessage(tgt, WM_MOUSEMOVE, 0, lp)
        time.sleep(0.02)
        win32gui.PostMessage(tgt, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.03)
        win32gui.PostMessage(tgt, WM_LBUTTONUP, 0, lp)
        return True
    except Exception as exc:
        dev_log("后台消息点击失败", exc)
        return False


def _post_key(top_hwnd, vk) -> bool:
    """后台按键:PostMessage WM_KEYDOWN/KEYUP(MaaNTE keyboard=PostMessage 同款)。发给窗口中心的最深子窗。"""
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


def _print_window_bgr(hwnd):
    """后台截图:PrintWindow(PW_CLIENTONLY|PW_RENDERFULLCONTENT)取窗口**自身**客户区画面
    (MaaNTE screencap=Background 同款思路)。被遮挡/无焦点也能取到真实内容;失败返回 None。"""
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
        ok = _u32.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(mem.GetSafeHdc()), 3)  # 1|2
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
    """拉起启动器 exe(本进程已是管理员,子进程随其清单决定权限,不再弹 UAC)。"""
    ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, os.path.dirname(path), 1)


def _norm1920(frame):
    """宽 > NORM_W 时按比例降采样到 NORM_W(ROI/点击都用归一化分数,缩放不影响坐标),省 OCR 时间。"""
    h, w = frame.shape[:2]
    if w <= NORM_W:
        return frame
    nh = max(1, int(round(h * NORM_W / w)))
    return cv2.resize(frame, (NORM_W, nh), interpolation=cv2.INTER_AREA)


def _crop(frame, roi):
    """按归一化 roi=(x0,y0,x1,y1) 切子图。"""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = roi
    return frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


class GameLauncher:
    TICK = 0.4                 # 主循环间隔(本任务靠 OCR,不需高频;加载阶段也无需抢帧)
    MIN_CONF = 0.5             # OCR 置信度下限(滤掉背景杂字)
    CLICK_DELAY = (0.2, 0.5)   # 每次点击**前**的随机延迟(秒;拟人,不固定节奏)
    IN_GAME_CONFIRM = 3        # 连续这么多 tick 无启动器/公告/开始游戏且未点过启动 → 判「已在游戏中」(非剧情状态),不启动
    START_STABLE_S = 2.0       # 「开始游戏」要连续稳定这么久才点 —— 避开"公告出现前的一瞬「开始游戏」闪现",防误触提前结束
    TOTAL_TIMEOUT_S = 300.0    # 整个任务上限(等启动器→加载→公告→开始游戏;着色器编译可能很久)
    LAUNCH_WAIT_S = 90.0       # 拉起 exe 后等启动器窗口出现的上限(启动器自己也要查更新/初始化)
    CLOSE_GRACE_S = 6.0        # 曾出现过窗口后又找不到任何游戏窗口,持续这么久 → 判定"用户已关闭" → 停止(防加载瞬断误停)
    AFTER_CLICK_S = 1.0        # 每次点击后的稳定等待

    # 归一化 ROI(x0,y0,x1,y1)与点击点 —— 见模块文档(坐标来自录制真值)
    ROI_LAUNCH_BTN = (0.74, 0.82, 0.97, 0.96)   # 启动器右下角大按钮(启动游戏/游戏中/退出中)
    ROI_ANNOUNCE = (0.10, 0.15, 0.38, 0.32)     # 进游戏「公告」弹窗左上标题(放宽)
    ROI_START = (0.30, 0.68, 0.70, 0.86)        # 正中偏下「开始游戏」(放宽以适配不同分辨率/窗口)
    LAUNCH_BTN_PT = (0.867, 0.892)
    CLOSE_X_PT = (0.808, 0.258)
    START_GAME_PT = (0.482, 0.820)
    VK_ESC = 0x1B                               # 公告:X 优先(实测点 X 能关),ESC 兜底交替
    # 加载/过场关键字(暖启动时区分"加载中=等" vs "游戏内=判已在游戏");来自实测加载帧 OCR
    LOADING_KEYS = ("初始化", "检测版本", "检测资源", "在进入", "正在进入", "进入游戏",
                    "着色", "编译", "Unreal", "Epic", "天美", "TiMi", "加载中", "请稍候", "图形")

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.success = False
        self._clicked_launch = False   # 本轮是否已点过「启动游戏」(点过 + 启动器按钮消失 = 真进了加载/游戏内)
        self._launch_cd = 0.0          # 点「启动游戏」防抖时间戳(这之前不重复点;点失败按钮还在则到点重试)
        self._saw_window = False   # 本轮是否出现过游戏/启动器窗口(出现过又消失 = 用户关闭 → 停止)
        self._driving_launcher = False  # 是否在驱动启动器(我们拉起的 / 识别到过启动器按钮)→ 登录/转圈期间一直等启动游戏,绝不判"已在游戏"
        self._announce_tries = 0   # 公告连续检测次数(第 1 次点 X,仍在则按 ESC 兜底)
        self._start_since = 0.0    # 「开始游戏」连续被检测到的起始时刻(用于稳定判定,0=当前没检测到)
        self._last_diag = 0.0      # 加载期诊断日志限频(记录开始游戏 ROI 实际 OCR 文字)
        self._launcher_hwnd = 0    # 点「启动游戏」那一刻的启动器 hwnd(游戏窗口=之后出现的**不同** hwnd)
        self._game_win_seen = False  # 点启动后是否出现过游戏窗口(新 hwnd);是→之后窗口全没/回到启动器=你退出了游戏
        self._exit_ready = 0       # "游戏退出后回到启动器「启动游戏」"的连续确认帧数(≥2 才停,防单帧 OCR 误读)
        self._flip = True          # 点击方式交替标记(首次=后台消息点击;同目标重试时隔次硬件兜底)
        self._start_clicked = 0    # 已点「开始游戏」次数(点后不立即判成功,等它从画面消失才算真进了游戏)
        self._hwnd = 0

    # ---- 生命周期 / 守卫 ----
    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = on

    def _stopped(self) -> bool:
        return bool(self.stop_flag)

    def _foreground(self) -> bool:
        """点击守卫(方案 B):允许点击的条件——
          · 游戏/启动器就是真前台(或焦点在它的 CEF 子窗口上);或
          · 前台是本程序/桌面(=用户点了开始在等自动启动),且目标窗口**视觉在最前**(落点上就是它)。
        用户在用第三方程序(第三方是前台)→ 一律拦下:不抢前台、不抢鼠标。"""
        h = self._hwnd
        if not h:
            return False
        fg = win32gui.GetForegroundWindow()
        if fg == h:
            return True
        if fg and _win_pid(fg) and _win_pid(fg) == _win_pid(h):   # CEF 子窗口拿着焦点 = 它就在最前
            return True
        if self._fg_owner_kind(fg) == "other":                    # 用户在用别的程序 → 不动
            return False
        return self._visible_state(h) == "top"

    def _fg_owner_kind(self, fg) -> str:
        """前台窗口归谁:'self'=本程序 / 'shell'=桌面·任务栏 / 'other'=第三方程序(用户在用,别打扰)。"""
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
        """h 客户区上要核验"视觉最前"的屏幕采样点:客户区中心 + 两个主要点击点(启动游戏/开始游戏)。"""
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
        """h 是否视觉最前:'top' 全部采样点都是它 / 'covered_self' 被本程序 GUI 挡住(可自己让开)/
        'covered_other' 被其它窗口挡住(绝不动别人 → 只能等)。"""
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
        """定位窗口并返回 (状态, hwnd)——后台模式(MaaNTE 同款):
          ('act', h)   窗口存在且未最小化 → **后台截图(PrintWindow)+ 后台消息点击(PostMessage)**,
                       不需要前台、不动真实鼠标、不怕遮挡 —— 你可以随意点本程序看日志/用别的程序;
          ('min', h)   最小化 → 暂停(最小化窗口常不渲染,取不到真实画面;恢复后自动继续);
          ('gone', 0)  没有任何游戏/启动器窗口 → 交上层判(加载空窗期 / 用户关闭)。"""
        fg = win32gui.GetForegroundWindow()
        if fg and _is_game_title(win32gui.GetWindowText(fg)):   # 前台恰好就是它 → 直接用
            self._hwnd = fg
            return "act", fg
        h = any_game_window()
        if not h:
            return "gone", 0
        if win32gui.IsIconic(h):                 # 最小化 → 暂停(用户主动最小化,不碰它)
            return "min", h
        self._hwnd = h
        return "act", h

    def _click(self, pt) -> None:
        """点击(后台优先,MaaNTE 同款):随机延迟(拟人)→ **PostMessage 后台消息点击**(不动真实鼠标、
        不需要前台、不怕遮挡)。同一目标重试时(消息点击对个别控件可能无效)**交替补一次硬件点击兜底**,
        但硬件路径只在"窗口视觉最前 + 前台不是第三方程序"时走(绝不打扰正在用别的程序的你)。
        任何点击失败只记日志、下一轮重试,不冒致命错误。"""
        if self.stop_flag:
            return
        time.sleep(random.uniform(*self.CLICK_DELAY))
        h = self._hwnd
        try:
            l, t, r, b = win32gui.GetClientRect(h)
            cw, ch = r - l, b - t
        except Exception as exc:
            dev_log("取客户区失败(窗口可能刚关闭)", exc)
            return
        if cw <= 0 or ch <= 0:
            return
        self._flip = not self._flip
        if self._flip and self._foreground():
            # 第 2/4/6…次重试且不打扰用户 → 硬件点击兜底(个别控件不吃窗口消息)
            try:
                safe_click_norm(h, pt, self._stopped, self._foreground, self.log, 0.02)
                return
            except Exception as exc:
                dev_log("硬件点击失败(转回后台消息点击)", exc)
        _post_click_client(h, int(pt[0] * cw), int(pt[1] * ch))

    def _press_esc_bg(self) -> None:
        """后台 ESC(PostMessage,不需要前台)。"""
        _post_key(self._hwnd, self.VK_ESC)

    def _press_esc(self) -> None:
        safe_press_key(self.VK_ESC, self._stopped, self._foreground, self.log, 0.05)

    # ---- OCR 取词 ----
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
        """OCR 该 ROI → [(text, cx_norm, cy_norm), ...](文字框中心按客户区归一化)。
        用框中心做点击点 → 随启动器窗口大小/位置自适应,比录制定值稳。"""
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
        """启动器右下角按钮 → (state, 点击点):ready(启动游戏)/ exiting / ingame / none。
        ready 的点击点取 OCR 命中「启动游戏」文字框中心(自适应窗口大小);取不到回退定值。"""
        lines = self._ocr_lines(f, self.ROI_LAUNCH_BTN)
        t = "".join(ln[0] for ln in lines)
        if "启动" in t:                       # 「启动游戏」——「启动」是它独有(游戏中/退出中都没有)
            pt = next(((cx, cy) for txt, cx, cy in lines if "启动" in txt), None)
            return "ready", (pt or self.LAUNCH_BTN_PT)
        if "退出" in t:                       # 「退出中」——上一局还在退,等
            return "exiting", None
        if "游戏中" in t or ("游戏" in t and "中" in t):
            return "ingame", None
        return "none", None

    def has_announcement(self, f) -> bool:
        return "公告" in self._ocr_join(f, self.ROI_ANNOUNCE)

    def find_start_game(self, f):
        """正中偏下「开始游戏」→ 文字框中心(归一化);没有则 None。
        必须**精确含「开始游戏」**(不松到「开始」「游戏」分别出现)—— 否则会误配启动器新闻栏里的零散字
        (实测启动器中下部新闻栏含「公告/活动/游戏」等字,松匹配会误点导致启动器滚动)。"""
        lines = self._ocr_lines(f, self.ROI_START)
        for txt, cx, cy in lines:
            if "开始游戏" in txt:
                return (cx, cy)
        joined = "".join(ln[0] for ln in lines)   # 容错:个别渲染把「开始游戏」拆成相邻两段
        if "开始游戏" in joined:
            return next(((cx, cy) for txt, cx, cy in lines if "开始" in txt or "游戏" in txt), self.START_GAME_PT)
        return None

    def _ensure_launcher_running(self) -> bool:
        """没有游戏/启动器窗口时:本地定位 exe → 拉起 → 等启动器窗口出现。成功返回 True。"""
        exe = find_game_exe()
        if not exe:
            self.log("未找到《王者荣耀世界》本地安装路径;请手动打开启动器,或在 data/config.json 的 game_path 指定 exe")
            return False
        self.log(f"未检测到游戏窗口 → 本地启动:{exe}")
        self._driving_launcher = True       # 我们拉起的启动器 → 之后会经历 登录/转圈,要一直等启动游戏
        try:
            launch_game_exe(exe)
        except Exception as exc:
            dev_log("拉起游戏 exe 失败", exc)
            self.log(f"启动失败:{type(exc).__name__}: {exc}")
            return False
        deadline = time.time() + self.LAUNCH_WAIT_S
        while not self.stop_flag and time.time() < deadline:
            if self.paused:
                time.sleep(0.2)
                continue
            if any_game_window():
                self.log("启动器已出现,开始识别")
                time.sleep(1.0)
                return True
            time.sleep(0.5)
        if not self.stop_flag:
            self.log("等待启动器窗口出现超时,已停止")
        return False

    # ---- 主流程:单循环按"当前界面状态"分派;只跑一轮,完成即返回,不重复启动 ----
    def run(self) -> bool:
        """每帧判当前是哪种界面 → 做对应动作。终态(已点开始游戏 / 一开始就已在游戏内)即返回结束。
        优先级:开始游戏 > 公告 > 启动器按钮 > (未点过启动且都不是)→ 已在游戏中。"""
        self.stop_flag = False
        self.success = False
        self._clicked_launch = False
        self._launch_cd = 0.0
        self._driving_launcher = False
        self._announce_tries = 0
        self._start_since = 0.0
        self._launcher_hwnd = 0
        self._game_win_seen = False
        self._exit_ready = 0
        self._flip = True
        self._start_clicked = 0
        self.log("自动启动游戏:开始(后台识别+后台点击:不需要前台、不动你的鼠标,"
                 "你可随意看日志/用别的程序;只跑一轮,完成即停;F12 急停)")
        # 没有任何游戏/启动器窗口 → 本地定位启动器 exe 并拉起(不必手动开启动器);等其窗口出现再进主循环
        if not any_game_window() and not self._ensure_launcher_running():
            return self.success
        idle_count = 0
        deadline = time.time() + self.TOTAL_TIMEOUT_S
        last = ""
        grace = None
        self._saw_window = False
        try:
            with GameCapture(0) as cap:        # GDI BitBlt(无黄框/无光标闪烁)
                while not self.stop_flag and time.time() < deadline:
                    if self.paused:
                        time.sleep(0.2)
                        continue
                    state, gw = self._resolve_window()
                    if state == "gone":
                        # 没有任何游戏窗口:
                        #  · 点过启动、**游戏窗口还没出现过** → 启动器关闭→游戏进程启动/着色器编译的空窗期 = 过渡
                        #    → 一直等游戏窗口出现,绝不当成"已关闭"而停(可能几十秒~几分钟)。
                        #  · 点过启动、**游戏窗口出现过**,现在又全没了 → 你在编译/加载/公告中退出了游戏
                        #    → **静默停止**(不报错、不重启);CLOSE_GRACE_S 宽限防窗口瞬断误判。
                        #  · 没点过启动但出现过窗口 → 用户关了启动器 → 停止。
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
                        self._game_win_seen = True   # 点启动后出现了**新的**王者窗口(hwnd 不同)= 游戏窗口已出现过
                    if state == "min":
                        # 最小化 → 暂停(最小化窗口常不渲染,后台也取不到真实画面;恢复后自动继续)
                        if last != "min":
                            self.log("启动器/游戏已最小化 → 暂停(恢复后自动继续)")
                            last = "min"
                        time.sleep(0.3)
                        continue
                    # 后台截图优先(PrintWindow:被遮挡/无焦点也取到窗口自身画面);
                    # 个别窗口 PrintWindow 不支持 → 仅当它**视觉最前**时退回屏幕截图(否则会截到盖在上面的别的窗口)
                    f = _print_window_bgr(gw)
                    if f is None and self._visible_state(gw) == "top":
                        cap.hwnd = gw
                        f = cap.grab()
                    if f is None:
                        time.sleep(self.TICK)
                        continue
                    fn = _norm1920(f)

                    # ===== 以"启动器按钮"为闸分派 =====
                    # 关键:**在启动器(按钮可见)时只认按钮、只点「启动游戏」**,绝不检查开始游戏/公告——
                    # 否则会误配启动器中下部"新闻栏"里的零散字 → 误点 → 启动器被滚动、却没进游戏。
                    btn, bpt = self.read_launch_button(fn)
                    if btn in ("ready", "exiting", "ingame"):
                        self._driving_launcher = True
                    if btn != "ready":
                        self._exit_ready = 0               # 不在「启动游戏」画面 → 退出确认计数清零

                    if btn == "ready":
                        # 点过启动、游戏窗口出现过,现在又回到启动器「启动游戏」= 你退出了游戏
                        # → 连续 2 帧确认后停止,**绝不重新启动**(2 帧防单帧 OCR 误读「启动」误停)。
                        if self._clicked_launch and self._game_win_seen:
                            self._exit_ready += 1
                            if self._exit_ready >= 2:
                                self.log("检测到游戏已退出、回到启动器 → 停止,不再重新启动")
                                return self.success
                            time.sleep(self.TICK)
                            continue
                        nowt = time.time()
                        if nowt >= self._launch_cd:        # 防抖:点完 5s 内不重复点;若没点中按钮还在,到点会重试
                            self.log("启动器就绪 → 点击「启动游戏」")
                            self._launcher_hwnd = self._hwnd   # 记录启动器 hwnd(之后出现的不同 hwnd = 游戏窗口)
                            self._click(bpt)
                            self._clicked_launch = True
                            self._launch_cd = nowt + 5.0
                            last = ""
                        elif last != "launching":
                            self.log("已点「启动游戏」,等待进入…")
                            last = "launching"
                        idle_count = 0
                        time.sleep(self.TICK)
                        continue
                    if btn == "exiting":
                        if last != "exiting":
                            self.log("启动器「退出中」→ 等待上一局退出后再启动…")
                            last = "exiting"
                        idle_count = 0
                        time.sleep(self.TICK)
                        continue
                    if btn == "ingame":
                        self.log("启动器显示「游戏中」→ 游戏已在运行,无需启动,直接结束")
                        self.success = True
                        return True

                    # —— 启动器按钮 none(不在启动器主页:登录 / 加载 / 公告 / 开始游戏 / 游戏内)——
                    # **先判公告/开始游戏,再判等待/登录**(否则暖启动见到公告设了 driving 后会误入"等登录"而忽略公告)
                    ann_txt = self._ocr_join(fn, self.ROI_ANNOUNCE)
                    start_lines = self._ocr_lines(fn, self.ROI_START)
                    if "公告" in ann_txt:
                        self._driving_launcher = True       # 见到公告 = 在进游戏序列(暖启动也据此持续等待)
                        self._start_since = 0.0             # 公告出现 → 开始游戏稳定计时清零(刚才那是公告前的闪现)
                        self._announce_tries += 1
                        if self._announce_tries % 2 == 1:   # X 优先(实测点 X 能关掉;ESC 对本游戏公告常无效)
                            self.log("检测到「公告」→ 点右上角 X 关闭")
                            self._click(self.CLOSE_X_PT)
                        else:
                            self.log("公告仍在 → 按 ESC 返回(后台按键)")
                            self._press_esc_bg()
                        time.sleep(0.8)
                        idle_count = 0
                        last = ""
                        continue
                    self._announce_tries = 0
                    sp = next(((cx, cy) for txt, cx, cy in start_lines if "开始游戏" in txt), None)
                    if sp:
                        # 「开始游戏」要**连续稳定 START_STABLE_S 秒**才点(避开公告前的瞬时闪现)。
                        # 点后**不立即判成功**:后台消息点击对个别控件可能无效 → 等它从画面消失才算真进了游戏;
                        # 仍在就隔 2s 重点(_click 会交替补硬件兜底,仅在不打扰你时)。
                        if self._start_since == 0.0:
                            self._start_since = time.time()
                        if (time.time() - self._start_since >= self.START_STABLE_S
                                and time.time() >= self._launch_cd):
                            self._start_clicked += 1
                            self.log(f"检测到「开始游戏」→ 点击进入游戏(第 {self._start_clicked} 次)")
                            self._click(sp)
                            self._launch_cd = time.time() + 2.0   # 复用防抖:2s 内不重复点
                            time.sleep(self.AFTER_CLICK_S)
                        elif last != "start_confirm":
                            self.log("检测到「开始游戏」,稳定确认中…(避开公告前的瞬时闪现)")
                            last = "start_confirm"
                        idle_count = 0
                        time.sleep(self.TICK)
                        continue
                    self._start_since = 0.0                 # 当前没检测到开始游戏 → 清零稳定计时
                    if self._start_clicked:
                        # 点过「开始游戏」且它已从画面消失(且无公告)= 已进入游戏 → 成功
                        self.on_count(1)
                        self.success = True
                        self.log("「开始游戏」已生效,进入游戏完成")
                        return True

                    # 既无公告也无开始游戏:判断"还在流程中(等)" vs "已在游戏内"
                    start_txt = "".join(ln[0] for ln in start_lines)
                    loading = any(k in (ann_txt + start_txt) for k in self.LOADING_KEYS)
                    if self._clicked_launch or self._driving_launcher or loading:
                        if loading:
                            self._driving_launcher = True   # 着色器/初始化等过场 → 持续等(暖启动也不再误判已在游戏)
                        if not self._clicked_launch and not loading:
                            # 冷启动登录 / 启动器就绪前(用户手动登录阶段)→ 不计入超时
                            deadline = time.time() + self.TOTAL_TIMEOUT_S
                            if last != "wait_launcher":
                                self.log("等待登录完成 / 启动器就绪…(登录界面请手动登录,完成后自动点「启动游戏」)")
                                last = "wait_launcher"
                        elif last != "loading":
                            self.log("加载中(初始化/检测版本/加载/着色器),不动作…")
                            last = "loading"
                        idle_count = 0
                        nowt = time.time()                  # 限频诊断:记录两区实际 OCR 文字
                        if nowt - self._last_diag > 3.0:
                            self._last_diag = nowt
                            dev_log(f"[launcher diag] 开始游戏ROI={start_txt!r} 公告ROI={ann_txt!r}")
                        time.sleep(self.TICK)
                        continue
                    idle_count += 1
                    if idle_count >= self.IN_GAME_CONFIRM:
                        self.log("当前已在游戏中(非启动器/非公告/非开始界面)→ 无需启动,直接结束")
                        self.success = True
                        return True
                    if last != "maybe_ingame":
                        self.log("未见启动器/公告/开始游戏,确认是否已在游戏中…")
                        last = "maybe_ingame"
                    time.sleep(self.TICK)
        finally:
            release_known_keys(self.log)
        if not self.stop_flag:
            self.log("自动启动游戏:超时结束(界面与预期不同,或加载过久)")
        return self.success


if __name__ == "__main__":
    GameLauncher().run()
