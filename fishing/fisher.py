"""HOKWord 钓鱼自动化引擎。

机制(由录制数据确认,与 MaaNTE 的滑块钓鱼不同——本作无滑块/张力):
  FISHING_READY 点击抛竿 → 等待 → 上钩啦 → 点击拉杆 → 自动收线 → 结算 → 续钓。
借鉴 MaaNTE 钓鱼结构:N 次循环、确保处于钓鱼态、超时重抛、结算 ESC 关闭、
缺饵不崩溃而停机告警、异常(未知界面/剧情)安全等待。

会发送真实鼠标点击,需以管理员运行(游戏提权,UIPI 会拦截普通权限的合成输入)。
仅在游戏前台时动作;支持随时停止;成功一条则把当前帧存到 SRC/屏幕截图/。
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import win32api
import win32con

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # 让 fisher 能 import recorder / fishing.matcher

from recorder import client_rect_on_screen, find_game_hwnd, is_foreground  # noqa: E402
from fishing.matcher import CLICK_POINT, FishingRecognizer  # noqa: E402

# 成功图保存到 SRC/屏幕截图/  (HERE=HOKWord/fishing -> parents[1]=SRC)
SCREENSHOT_DIR = HERE.parents[1] / "屏幕截图"

# 落杆错误提示的中文原因(日志用)
_CAST_REASON = {
    "too_close": "落点过近", "too_far": "超出落杆范围",
    "not_water": "落点不在水面", "shallow": "水域深度不足",
}


class FishingBot:
    def __init__(self, log=print, on_count=lambda n: None, debug=True) -> None:
        self.rec = FishingRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.caught = 0
        self.debug = debug          # 调试期:抓上钩/结算帧到 sessions/_debug/run_*
        self._dbgdir = None
        self._last_qdbg = 0.0       # QTE 审计抓帧限频
        self.cast_pt = list(CLICK_POINT)  # 落杆点(可按"过近/过远"提示动态调整)

    def stop(self) -> None:
        self.stop_flag = True

    def _dbg(self, frame, tag: str) -> None:
        if not self.debug or frame is None or self._dbgdir is None:
            return
        try:
            cv2.imwrite(str(self._dbgdir / f"{tag}_{time.strftime('%H%M%S')}_{time.perf_counter()%100:.2f}.jpg"),
                        frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception:
            pass

    def _press_f(self) -> None:
        win32api.keybd_event(0x46, 0, 0, 0)   # F:放入背包/确认
        time.sleep(0.05)
        win32api.keybd_event(0x46, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _dbg_qte(self, frame, tag: str) -> None:
        """QTE 审计抓帧(限频 ~4/s):记录每刻画面 + 机器人判定,供事后核对。"""
        now = time.perf_counter()
        if now - self._last_qdbg >= 0.25:
            self._dbg(frame, tag)
            self._last_qdbg = now

    _VK = {"A": 0x41, "D": 0x44, "W": 0x57, "S": 0x53, "F": 0x46}

    # 大鱼 QTE 点按:本作大鱼**只能靠点按**消耗耐久,按住反而脱钩。
    # 故用「快速短按」并尽量高频:按下保持极短再抬起,间隔很小(略带随机,拟人)。
    TAP_DOWN_S = 0.012                # 单次按下保持(够游戏识别,又尽量短)
    TAP_GAP = (0.006, 0.022)          # 两次点按间隔(短 → 高频)
    # 离散 QTE(SADW 大按钮)按键随机延迟:新按钮出现后等一随机时长再按,使落点散布在
    # 按钮窗口前 ~3/4(拟人,避免每次都在 1/4 处秒按)。须 < 按钮超时窗口(实测窗口≥~1.5s)。
    DISC_PRESS_DELAY = (0.30, 0.95)

    def _press_key(self, k: str) -> None:
        vk = self._VK.get(k.upper())
        if not vk:
            return
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

    def _tap(self, k: str) -> None:
        """大鱼 QTE 用的快速点按(按下→极短保持→抬起→很小间隔)。"""
        vk = self._VK.get(k.upper())
        if not vk:
            return
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(self.TAP_DOWN_S)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(random.uniform(*self.TAP_GAP))

    def _release_all(self) -> None:
        """急停/退出时把方向键全部抬起,防止某次点按的抬键漏发导致角色卡键乱走。"""
        for k in ("A", "D", "W", "S"):
            try:
                win32api.keybd_event(self._VK[k], 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

    # ---- 有界随机延迟 ----
    # 约束:总延迟(系统+画面+识别+随机)≤ 钓鱼失败时间的 2/3。
    # 上钩窗口实测约 0.5s → 2/3≈0.33s;固定反应(抓帧~12ms+识别~1ms+点击~80ms+轮询~50ms)≈0.14s,
    # 故上钩随机延迟上限 ≈0.10s(总 ~0.24s < 0.33s)。抛竿/放入背包非时间敏感,可较大。
    HOOK_FAIL_S = 0.5
    HOOK_FIXED_S = 0.14

    def _delay_hook(self) -> None:
        budget = self.HOOK_FAIL_S * 2 / 3 - self.HOOK_FIXED_S    # ≈0.19s 余量
        time.sleep(random.uniform(0.0, max(0.0, min(0.10, budget))))

    def _delay_action(self) -> None:
        time.sleep(random.uniform(0.10, 0.45))   # 放入背包 F 等:拟人化随机

    def _delay_cast(self) -> None:
        time.sleep(random.uniform(0.5, 3.0))     # 抛竿:0.5~3s 随机延迟(更拟人)

    # ---- 底层 ----
    def _grab(self, sct, hwnd):
        x, y, w, h = client_rect_on_screen(hwnd)
        if w <= 0 or h <= 0:
            return None
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        return np.asarray(shot)[:, :, :3]

    def _click(self, hwnd, pt=None) -> None:
        pt = pt if pt is not None else self.cast_pt
        x, y, w, h = client_rect_on_screen(hwnd)
        sx, sy = int(x + pt[0] * w), int(y + pt[1] * h)
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.04)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.04)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _esc(self) -> None:
        win32api.keybd_event(0x1B, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(0x1B, 0, win32con.KEYEVENTF_KEYUP, 0)

    @staticmethod
    def _water_center(frame) -> list:
        """重新检测水域中央(蓝青色大片区域质心),用作落杆点兜底。检测失败回中心默认值。
        只在多次方向微调仍失败时调用,失败也安全(随后会因尝试超次而停机)。"""
        h, w = frame.shape[:2]
        y0 = int(0.20 * h)
        band = cv2.cvtColor(frame[y0:int(0.66 * h), :], cv2.COLOR_BGR2HSV)
        H, S, V = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        mask = (((H > 80) & (H < 140)) & (V > 40) & (V < 235)).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            c = max(cnts, key=cv2.contourArea)
            m = cv2.moments(c)
            if m["m00"] > 0.04 * mask.size:           # 区域够大才信
                cx = (m["m10"] / m["m00"]) / w
                cy = (m["m01"] / m["m00"] + y0) / h
                return [float(np.clip(cx, 0.32, 0.68)), float(np.clip(cy, 0.34, 0.58))]
        return [0.50, 0.45]

    def _foreground_ok(self, hwnd) -> bool:
        """等待游戏回到前台(切走时自动暂停动作)。返回 False = 用户已停止。"""
        warned = False
        while not is_foreground(hwnd):
            if self.stop_flag:
                return False
            if not warned:
                self.log("游戏不在前台,暂停动作…")
                warned = True
            time.sleep(0.3)
        return True

    def _classify(self, sct, hwnd):
        f = self._grab(sct, hwnd)
        if f is None:
            return "NO_FRAME", None, {}
        st, sc = self.rec.classify(f)
        return st, f, sc

    # ---- 状态步骤 ----
    def _save_debug(self, frame, tag, scores=None) -> None:
        d = HERE.parent / "sessions" / "_debug"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tag}_{time.strftime('%H%M%S')}.png"
        cv2.imwrite(str(p), frame)
        self.log(f"调试帧已存: sessions/_debug/{p.name}" + (f"  scores={scores}" if scores else ""))

    def _ensure_ready(self, sct, hwnd, timeout=20.0) -> bool:
        start = time.time()
        last_log = 0.0
        last = None
        while time.time() - start < timeout:
            if self.stop_flag or not self._foreground_ok(hwnd):
                return False
            st, f, sc = self._classify(sct, hwnd)
            last = (f, sc)
            if time.time() - last_log > 2.0:
                self.log(f"等待预备态… 识别={st} ready={sc.get('ready')} "
                         f"wait={sc.get('wait')} banner={sc.get('banner')} 黑={sc.get('black_mean')}")
                last_log = time.time()
            if st == "FISHING_READY":
                return True
            if f is not None and self.rec.is_record_screen(f):   # 残留个人新纪录 → F(不按 ESC)
                self._press_f()
                time.sleep(0.5)
            time.sleep(0.2)
        if last and last[0] is not None:  # 失败存帧供分析
            self._save_debug(last[0], "ready_fail", last[1])
        return False

    @staticmethod
    def _is_black(frame) -> bool:
        return float(cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2GRAY).mean()) < 12.0

    def _confirm_cast(self, sct, hwnd, timeout=5.0) -> bool:
        """抛竿后确认进入"等待咬钩"(取消按钮,winner-take-all 区分预备/等待)。
        失败=距离过远/抛竿没成功。"""
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag or not is_foreground(hwnd):
                return False
            st, _, _ = self._classify(sct, hwnd)
            if st == "WAITING_FOR_BITE":
                return True
            time.sleep(0.15)
        return False

    def _wait_hook(self, sct, hwnd, timeout=30.0) -> str:
        """快速等待上钩(单/小尺度横幅检测,低延迟)。返回 'hook'/'settle'/'timeout'/'stop'。"""
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag or not self._foreground_ok(hwnd):
                return "stop"
            f = self._grab(sct, hwnd)
            if f is None:
                continue
            if self.rec.is_hook(f):
                self._dbg(f, "hookfire")
                return "hook"
            if self._is_black(f):       # 意外结算/过渡黑场
                return "settle"
            time.sleep(0.03)            # 上钩窗口短,尽量低延迟
        return "timeout"

    def _save_success(self, frame) -> None:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = SCREENSHOT_DIR / f"钓鱼成功_{time.strftime('%Y%m%d_%H%M%S')}_{self.caught + 1}.png"
        cv2.imwrite(str(name), frame)
        self.log(f"成功图已存: 屏幕截图/{name.name}")

    def _resolve_outcome(self, sct, hwnd, timeout=16.0) -> str:
        """拉杆后判结果。成功=出现渔获奖励飘字(垂钓家经验/鱼*1);收线+特殊界面
        (个人新纪录 / 放入背包F)期间周期性按 F+ESC 推进,只在非钓鱼态时按(防误开菜单)。
        回到预备/等待即结束。返回 'success' / 'escape' / 'stop'。"""
        start = time.time()
        success = False
        n = 0
        while time.time() - start < timeout:
            if self.stop_flag:
                return "stop"
            if not is_foreground(hwnd):
                time.sleep(0.2)
                continue
            f = self._grab(sct, hwnd)
            if f is None:
                continue
            if n % 3 == 0:
                self._dbg(f, "outcome")   # 抓整个结算过程供调试/标定
            n += 1
            if not success and self.rec.is_success(f):
                self._save_success(f)
                success = True
                self.log("✓ 检测到渔获奖励")
            # 仅当确为"个人新纪录"界面(右上徽标 + 右下F放入背包)才按 F 放入背包;绝不按 ESC
            if self.rec.is_record_screen(f):
                self.log("个人新纪录 → 按 F 放入背包")
                self._press_f()
                time.sleep(0.5)
                continue
            st, _, _ = self._classify(sct, hwnd)
            if st in ("FISHING_READY", "WAITING_FOR_BITE"):
                return "success" if success else "escape"
            time.sleep(0.12)
        return "success" if success else "escape"

    def _back_to_ready(self, sct, hwnd, timeout=8.0) -> None:
        """确保回到预备态。仅"个人新纪录"界面按 F;绝不按 ESC。"""
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag:
                return
            f = self._grab(sct, hwnd)
            if f is not None and self.rec.is_record_screen(f):
                self._press_f()
                time.sleep(0.5)
                continue
            st, _, _ = self._classify(sct, hwnd)
            if st in ("FISHING_READY", "WAITING_FOR_BITE"):
                return
            time.sleep(0.3)

    # ---- 主循环(状态机:每帧判别当前状态并执行对应动作,可从任意状态接续/恢复)----
    def run(self, count: int = 10, exit_after: bool = False) -> None:
        self.stop_flag = False
        self.caught = 0
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        if self.debug:
            self._dbgdir = HERE.parent / "sessions" / "_debug" / f"run_{time.strftime('%H%M%S')}"
            self._dbgdir.mkdir(parents=True, exist_ok=True)
            self.log(f"调试抓帧 → sessions/_debug/{self._dbgdir.name}")
        self.log(f"开始钓鱼,目标 {count} 条")
        self.log("5 秒后开始 — 请切到游戏并站在钓鱼点(已持竿、可抛竿的预备态)")
        for _ in range(5):
            if self.stop_flag:
                self.log("已取消")
                return
            time.sleep(1)

        pulled = False          # 已拉杆,等待结算判定(成功/脱钩)
        pull_t = 0.0
        current_ad = None       # 快速连点态当前方向键(A/D);小键帽消失即清
        last_rapid = 0.0        # 上次见到连点小键帽的时刻(短迟滞,桥接漏检)
        disc_key = None         # 离散 QTE 当前按钮字母(去抖:同字母不重复狂点)
        disc_press_at = 0.0     # 该按钮的计划按下时刻(= 出现时刻 + 随机延迟)
        disc_pressed = False    # 该按钮是否已按过(防重复;漏按则慢节奏补按)
        last_disc_press = 0.0   # 上次离散按键时刻
        end_streak = 0          # 连续多帧"回到普通钓鱼按钮"= 本鱼结束
        qframe = 0              # 拉杆后帧计数(用于节流较慢的记录界面检测)
        last_cast = 0.0
        last_progress = time.time()
        IDLE_STOP_S = 60.0      # 持续无可识别钓鱼状态则停(可能离开钓点/异常)
        # 抛竿看门狗(缺饵/朝向不对/钓点异常会导致空抛进不了"等待咬钩")
        t_start = time.time()
        cast_count = 0          # 总抛竿次数(统计)
        cast_pending = False    # 已抛竿但还没确认进入"等待咬钩"
        cast_t = 0.0            # 本次抛竿时刻
        consec_cast_fail = 0    # 连续"抛竿未进入等待"次数
        CAST_CONFIRM_S = 4.0    # 抛竿后多久内须出现"取消"才算成功(正常 <1s)
        MAX_CAST_FAIL = 5       # 连续这么多次空抛 → 疑似缺饵/异常,停机告警
        self.cast_pt = list(CLICK_POINT)  # 每轮重置落杆点
        cast_adjust = 0         # 本钓点"过近/过远"已调整次数
        err_checked = False     # 本竿是否已查过落杆错误提示(避免重复 OCR)
        MAX_ADJUST = 3          # 落杆位置最多调整 3 次,仍失败则停机
        CAST_DY = 0.06          # 每次方向微调的 y 步长(过近上移/过远下移)
        ERR_CHECK_S = 0.8       # 抛竿后多久查一次错误提示(够提示弹出)
        with mss.mss() as sct:
          try:
            while self.caught < count and not self.stop_flag:
                if not self._foreground_ok(hwnd):
                    break
                f = self._grab(sct, hwnd)
                if f is None:
                    time.sleep(0.03)
                    continue

                # 1) 上钩(最高优先,时间敏感)——未处于拉杆后等待时才接
                if not pulled and self.rec.is_hook(f):
                    self._dbg(f, "hookfire")
                    self._delay_hook()
                    self._click(hwnd)
                    self.log("上钩 → 拉杆")
                    pulled = True
                    pull_t = time.time()
                    current_ad = None
                    end_streak = 0
                    last_progress = time.time()
                    time.sleep(0.25)
                    continue

                # 2) 拉杆后:特殊收线。两种态交替——
                #    · 快速连点(小白键帽 [A]/[D] + "快速连点"字样):对该方向**高频连点**消耗耐久(按住会脱钩);
                #    · 离散 QTE(深灰大圆按钮 + 亮白字母 A/S/W/D):每个按钮**只按一次**,按完等下一个。
                #    A) 钓鱼按钮恢复 = 本鱼结束(停手,预备态 A/D 是移动键会挪走角色)
                #    B) 记录鱼界面(节流检测)= 计成功
                #    C) 离散大按钮优先:识别字母按一次(失败/超时同形,慢节奏补按)
                #    D) 否则连点小键帽:高频连点(短迟滞桥接漏检)
                #    E) 否则(连点↔QTE 间的水花过渡 / 普通鱼无 QTE):等待 + 查渔获 + 超时兜底
                if pulled:
                    qframe += 1
                    bs, _ = self.rec.button_state(f)
                    # A) 钓鱼按钮恢复 = 本鱼结束:停手,判成功/脱钩
                    if bs in ("ready", "wait"):
                        current_ad = None
                        disc_key = None
                        end_streak += 1
                        if end_streak >= 2:
                            if self.rec.is_success(f):
                                self._save_success(f)
                                self.caught += 1
                                self.on_count(self.caught)
                                self.log(f"✓ 钓到! 已钓 {self.caught}/{count}")
                            else:
                                self.log("✗ 脱钩/结束,重抛")
                            pulled = False
                            end_streak = 0
                            last_progress = time.time()
                        time.sleep(0.03)
                        continue
                    end_streak = 0

                    # B) 记录鱼界面(较慢,~每 6 帧查一次)→ 计成功,下方按 F 关闭
                    if qframe % 6 == 0 and self.rec.is_record_screen(f):
                        current_ad = None
                        disc_key = None
                        self._dbg(f, "record_caught")
                        self._save_success(f)
                        self.caught += 1
                        self.on_count(self.caught)
                        self.log(f"✓ 钓到记录鱼! 已钓 {self.caught}/{count}")
                        pulled = False
                        last_progress = time.time()
                        continue

                    now = time.time()
                    # C) 离散 QTE 大按钮:识别字母 → **随机延迟后按一次**(拟人,落在窗口前 ~3/4)。
                    #    失败/超时按钮同形;若漏按(按钮仍在)则慢节奏补按。绝不连续点。
                    dk = self.rec.qte_disc(f)
                    if dk:
                        current_ad = None            # 离散态绝不连点
                        if dk != disc_key:           # 新按钮:排一个随机延迟再按
                            disc_key = dk
                            disc_press_at = now + random.uniform(*self.DISC_PRESS_DELAY)
                            disc_pressed = False
                        if now >= disc_press_at and (not disc_pressed or (now - last_disc_press) > 0.45):
                            self._press_key(dk)
                            self.log(f"QTE 按键:{dk}")
                            disc_pressed = True
                            last_disc_press = now
                        last_progress = now
                        time.sleep(0.02)
                        continue
                    disc_key = None                  # 大按钮消失 → 下一个(哪怕同字母)当新按钮处理

                    # D) 快速连点态:小白键帽出现 → 对该方向高频连点(仅小键帽近期可见时,防离散/等待乱点)
                    qk = self.rec.qte_key(f)
                    if qk:
                        if qk != current_ad:
                            self.log(f"快速连点:{qk}")
                            current_ad = qk
                        last_rapid = now
                        last_progress = now
                    if current_ad in ("A", "D") and now - last_rapid <= 0.30:
                        self._tap(current_ad)
                        if now - last_progress > 15.0:
                            self.log("✗ 收线超时,重抛")
                            current_ad = None
                            pulled = False
                        continue
                    current_ad = None                # 小键帽久未见 → 停连点

                    # E) 等待/无提示(水花过渡 / 普通鱼无 QTE)→ 查渔获 + 超时兜底
                    if self.rec.is_success(f):
                        self._save_success(f)
                        self.caught += 1
                        self.on_count(self.caught)
                        self.log(f"✓ 钓到! 已钓 {self.caught}/{count}")
                        pulled = False
                        last_progress = time.time()
                        time.sleep(0.15)
                        continue
                    if now - last_progress > 15.0:
                        self.log("✗ 脱钩(超时),重抛")
                        pulled = False
                    time.sleep(0.03)
                    continue

                # 3) 个人记录/渔获详情界面(F 放入背包)→ 按 F 回正常钓鱼
                if self.rec.is_record_screen(f):
                    self._dbg(f, "record")
                    self._delay_action()
                    self._press_f()
                    self.log("个人记录 → F 放入背包")
                    last_progress = time.time()
                    time.sleep(0.5)
                    continue

                # 4) 快速按钮状态 → 抛竿 / 等待
                bs, _ = self.rec.button_state(f)
                if bs == "ready":
                    if pulled:                       # 拉杆后回到预备态却无渔获 = 脱钩
                        self.log("✗ 脱钩,未钓到,重抛")
                        pulled = False
                    now = time.time()
                    # 抛竿后查"落杆错误提示":过近/过远 → 自动调整落点重试;其他/等级上限 → 按计划停机
                    if cast_pending and not err_checked and now - cast_t > ERR_CHECK_S:
                        err_checked = True
                        if self.rec.is_level_cap(f):
                            self._save_debug(f, "level_cap")
                            self.log("已达等级上限(脚本不处理),停机")
                            break
                        err = self.rec.cast_error(f)
                        if err in ("too_close", "too_far"):
                            cast_adjust += 1
                            if cast_adjust > MAX_ADJUST:
                                self._save_debug(f, "cast_adjust_fail")
                                self.log(f"落杆位置调整 {MAX_ADJUST} 次仍失败({_CAST_REASON[err]}),停机")
                                break
                            if cast_adjust >= MAX_ADJUST:               # 末次:重新检测水域中央
                                self.cast_pt = self._water_center(f)
                                self.log(f"落杆{_CAST_REASON[err]} → 重定位水域中央(第{cast_adjust}/{MAX_ADJUST}次)")
                            else:                                       # 方向微调:过近上移/过远下移 + 向水平中心靠
                                dy = -CAST_DY if err == "too_close" else CAST_DY
                                ny = min(0.70, max(0.30, self.cast_pt[1] + dy))
                                self.cast_pt = [self.cast_pt[0] * 0.6 + 0.5 * 0.4, ny]
                                self.log(f"落杆{_CAST_REASON[err]},{'上移' if err == 'too_close' else '下移'}重试(第{cast_adjust}/{MAX_ADJUST}次)")
                            cast_pending = False
                            last_cast = 0.0                              # 立即重抛
                        elif err in ("not_water", "shallow"):
                            self._save_debug(f, "cast_err")
                            self.log(f"落杆失败:{_CAST_REASON[err]}(脚本不处理),停机")
                            break
                    # 看门狗:超时仍未进入"等待咬钩"(无错误提示的空抛,疑似缺饵)
                    if cast_pending and now - cast_t > CAST_CONFIRM_S:
                        consec_cast_fail += 1
                        cast_pending = False
                        self.log(f"抛竿未进入等待({consec_cast_fail}/{MAX_CAST_FAIL})")
                        if consec_cast_fail >= MAX_CAST_FAIL:
                            self._save_debug(f, "cast_fail")
                            self.log("连续多次抛竿无效,疑似缺饵/朝向不对/钓点异常,停机")
                            break
                    # 仅在无未决抛竿时才抛(避免空抛不断刷新计时、看门狗永不触发)
                    if not cast_pending and now - last_cast > 2.0:
                        self._delay_cast()           # 抛竿前 0.5~3s 随机延迟
                        self._click(hwnd)
                        cast_count += 1
                        self.log(f"抛竿(目标 {self.caught + 1}/{count})")
                        t_now = time.time()          # 延迟后取新时间,避免看门狗按延迟前计时误判
                        last_cast = t_now
                        cast_t = t_now
                        cast_pending = True
                        err_checked = False
                        last_progress = t_now
                    time.sleep(0.15)
                elif bs == "wait":
                    if cast_pending:                 # 抛竿成功进入等待 → 清看门狗 + 落点有效
                        cast_pending = False
                        consec_cast_fail = 0
                        cast_adjust = 0
                    last_progress = time.time()
                    time.sleep(0.05)                 # 等咬钩:高频轮询,便于快速接上钩
                else:                                # none/收线/过渡/未知
                    if pulled and time.time() - pull_t > 12.0:   # 拉杆后久无结果 = 脱钩兜底
                        self.log("✗ 脱钩(超时未见渔获),重抛")
                        pulled = False
                    if time.time() - last_progress > IDLE_STOP_S:
                        self._save_debug(f, "stuck")
                        self.log("长时间无可识别钓鱼状态,停止(可能离开钓点/异常界面)")
                        break
                    time.sleep(0.1)
          finally:
            self._release_all()   # 任何退出(停止/异常/F12)都抬起方向键,避免漏发抬键卡键/角色乱走
        dt = time.time() - t_start
        mins = dt / 60.0
        rate = (self.caught / mins) if mins > 0 else 0.0
        self.log(f"钓鱼结束,共钓到 {self.caught} 条 · 抛竿 {cast_count} 次 · "
                 f"用时 {int(dt // 60)}分{int(dt % 60)}秒 · 约 {rate:.1f} 条/分")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    FishingBot().run(n)
