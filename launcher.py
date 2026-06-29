"""launcher.py — 自动启动游戏(启动器 → 进入游戏),对齐 MaaNTE/okww 的"识别→点击"流水。

**为什么不能像剧情/采集那样绑定单一窗口**:启动器与游戏窗口**同标题**「王者荣耀世界」但 **hwnd 不同**
(录制实测 920228=启动器 → 4850284=游戏);点「启动游戏」后窗口会更换。故本模块每帧**跟随当前前台窗口**
(recorder.foreground_target),只要标题含「王者荣耀世界」就锁定它来截图/点击。

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
import win32con
import win32gui

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


def _bring_to_front(h) -> None:
    """把游戏/启动器窗口置前台(本程序点开始后是本程序在前台 → 不置前台会被遮挡:截不到、点不中)。
    SetForegroundWindow 受系统限制时,用附加前台线程输入队列绕过。"""
    try:
        if win32gui.IsIconic(h):
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(h)
        return
    except Exception:
        pass
    try:
        import win32process
        cur = win32gui.GetForegroundWindow()
        t_cur = win32process.GetWindowThreadProcessId(cur)[0]
        t_tgt = win32process.GetWindowThreadProcessId(h)[0]
        if t_cur and t_tgt and t_cur != t_tgt:
            win32process.AttachThreadInput(t_cur, t_tgt, True)
            try:
                win32gui.SetForegroundWindow(h)
            finally:
                win32process.AttachThreadInput(t_cur, t_tgt, False)
    except Exception as exc:
        dev_log("launcher 置前台失败", exc)


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
        """点击守卫:当前前台就是我们锁定的游戏/启动器窗口(已置前台才点,避免点到本程序/别处)。"""
        return bool(self._hwnd and win32gui.GetForegroundWindow() == self._hwnd)

    def _resolve_window(self):
        """定位窗口并返回 (状态, hwnd):
          ('act', h)   游戏/启动器**已在前台**(你点了它)→ 截图/点击;
          ('min', h)   最小化 → 暂停;
          ('wait', h)  游戏在后台(前台是本程序/桌面/别的程序)→ 暂停,**绝不抢焦点**;
          ('gone', 0)  窗口没了(用户关闭)→ 停止。
        关键:**脚本绝不主动把游戏切回前台。只有游戏/启动器窗口自己在前台(你点了它、或启动器刚拉起自动到前台)
        时才操作;你点本软件 / 切到桌面 / 切到别的程序 → 一律暂停,等你点回游戏窗口再继续。**"""
        fg = win32gui.GetForegroundWindow()
        if fg and _is_game_title(win32gui.GetWindowText(fg)):   # 游戏/启动器在前台(你点了它)→ 干活
            self._hwnd = fg
            return "act", fg
        h = any_game_window()
        if not h:
            return "gone", 0
        if win32gui.IsIconic(h):                 # 最小化 → 暂停
            return "min", h
        return "wait", h                         # 游戏在后台 → 暂停,绝不抢焦点;你点游戏窗口它才继续

    def _click(self, pt) -> None:
        """点击前加 0.2-0.5s 随机延迟(拟人),再直接定位 + 立即点击(走 runtime_guard 守卫:
        停止/前台检查 + 失败急停)。停止时跳过延迟尽快退出。"""
        if not self.stop_flag:
            time.sleep(random.uniform(*self.CLICK_DELAY))
        safe_click_norm(self._hwnd, pt, self._stopped, self._foreground, self.log, 0.02)

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
        self.log("自动启动游戏:开始(按当前界面状态决定动作;只跑一轮,完成即停;仅前台时动作;F12 急停)")
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
                        #  · **已点过启动游戏** → 启动器关闭、游戏着色器编译期间暂时无「王者荣耀世界」窗口 = 过渡/加载
                        #    → 一直等游戏窗口出现,**绝不当成"已关闭"而停**(着色器编译可能几十秒~几分钟)。
                        #  · 还没点启动游戏、但之前出现过窗口 → 用户主动关了启动器 → 停止(留 CLOSE_GRACE_S 宽限)。
                        if self._clicked_launch:
                            if last != "transition":
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
                    if state in ("min", "wait"):
                        # min=最小化;wait=你切到了别的窗口。都暂停、不抢焦点(回到游戏/启动器窗口即自动继续)。
                        if last != state:
                            self.log("启动器/游戏已最小化 → 暂停(恢复后自动继续)" if state == "min"
                                     else "你切到了其它窗口 → 暂停(回到游戏/启动器窗口后自动继续)")
                            last = state
                        time.sleep(0.3)
                        continue
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

                    if btn == "ready":
                        nowt = time.time()
                        if nowt >= self._launch_cd:        # 防抖:点完 5s 内不重复点;若没点中按钮还在,到点会重试
                            self.log("启动器就绪 → 点击「启动游戏」")
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
                            self.log("公告仍在 → 按 ESC 返回")
                            self._press_esc()
                        time.sleep(0.8)
                        idle_count = 0
                        last = ""
                        continue
                    self._announce_tries = 0
                    sp = next(((cx, cy) for txt, cx, cy in start_lines if "开始游戏" in txt), None)
                    if sp:
                        # 「开始游戏」要**连续稳定 START_STABLE_S 秒**才点:公告出现前会有一瞬「开始游戏」闪现,
                        # 直接点会误触、公告随后盖上 → 任务提前结束。稳定期内若公告冒出会被上面分支接管并清零。
                        if self._start_since == 0.0:
                            self._start_since = time.time()
                        if time.time() - self._start_since >= self.START_STABLE_S:
                            self.log("检测到「开始游戏」→ 点击进入游戏")
                            self._click(sp)
                            time.sleep(self.AFTER_CLICK_S)
                            self.on_count(1)
                            self.success = True
                            self.log("已点击「开始游戏」,进入游戏完成")
                            return True
                        if last != "start_confirm":
                            self.log("检测到「开始游戏」,稳定确认中…(避开公告前的瞬时闪现)")
                            last = "start_confirm"
                        idle_count = 0
                        time.sleep(self.TICK)
                        continue
                    self._start_since = 0.0                 # 当前没检测到开始游戏 → 清零稳定计时

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
