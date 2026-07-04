"""HOKWord 实时采集引擎:跑图经过材料 → 识别 F 提示 → 按 F 采集。

**默认即时采集(快)**:每帧只做「图标分类」(模板匹配 ~3ms,无 OCR)判断有没有可采提示;
只有在**需要核对名字时**才 OCR(~150ms)按白/黑名单决定按不按。重现图标唯一、无碰撞,免 OCR 直接按。

**多个材料同屏 / 一闪而过也能快速清场(参考原神 BetterGI 自动拾取的"持续按 F"思路)**:
王者同屏多个可采物时只显示**最近一个**的「F+名称」提示,按 F 收掉最近的、提示立刻跳到下一个——
所以不能"一个提示只按一次",而要**只要还是可采的手型/重现提示就按节奏连按 F**(RETRY_GAP 很短),
把同屏/快速串行出现的材料一次清完;提示一消失(ABSENT_RESET)就复位。

兼顾黑名单安全(铸星/渡石等手型却不该采):
  · 提示刚出现(上升沿)→ OCR 决策一次:可采→按 F;碰撞名单命中→标记"跳过"且**之后不再 OCR 空转**。
  · 标记"可采"的提示持续存在 → 每 RETRY_GAP **重新 OCR 复核一次**再按(这样同屏串行里混进的黑名单项
    会被复核拦下、停手),既快又不会误采黑名单。
  · 标记"跳过"的提示持续存在 → 原地不动、不再 OCR(站在铸星/渡石前不空耗),离开/消失后复位。
  · 优先级:白名单(强制采)> 黑名单(碰撞跳)> 图标默认(手型/重现采、其它跳)。

按 F 用 win32 合成输入,需以管理员运行(游戏提权,UIPI 会拦普通权限的合成输入)。
复用 winenv 的窗口/前台/截图 与 1920 归一化。可单独自测:  python -m gather.picker
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import find_game_hwnd, is_admin, is_foreground  # noqa: E402
from capture import GameCapture  # noqa: E402
from gather.recognizer import GATHER_REGION, GatherRecognizer  # noqa: E402
from runtime_guard import dev_log, release_known_keys, safe_press_key  # noqa: E402

VK_F = 0x46


class GatherPicker:
    DETECT_INTERVAL = 0.02     # 轮询间隔(区域截图+图标分 ~12ms;截图提速后调快,更快抓到一闪而过的提示)
    IDLE_INTERVAL = 0.06       # 空闲轮询(屏幕上没有任何 F 提示 → ~16Hz 慢扫,省 CPU/GDI 让给游戏;
                               # 新提示最多晚 ~40ms 发现,而按 F 前本就要 OCR ~150ms 核名 → 无采集损失)
    IDLE_AFTER = 1.5           # 无提示这么久才降频(材料密集区提示接连出现 → 一直满速清场)
    RETRY_GAP = 0.25          # 同一提示持续存在时的连按节奏(收掉最近的、提示跳到下一个 → 接着按,清场)
    MAX_PRESS = 20            # 单段提示连续按 F 的上限(防某个"采不掉的手型"无限连按;正常一段几下就清完)
    ABSENT_RESET = 0.3       # 提示消失这么久才算"结束";再出现当新提示(去抖,防闪烁误触发)
    NOTEXT_MAX = 4           # 手型提示名字没读清时,重读上限;超了仍读不出 → 视作跳过(宁可漏采不误采黑名单)
    NOTEXT_GAP = 0.02        # 没读清的重读间隔(很短,几乎下一帧就重读)→ 把"读不清"延迟压到约一次 OCR

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.rec = GatherRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.picked = 0
        self._hwnd = None

    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = on

    def _press_f(self) -> None:
        safe_press_key(VK_F, self._stopped, self._foreground, self.log, 0.05)

    def _stopped(self) -> bool:
        return bool(self.stop_flag)

    def _foreground(self) -> bool:
        return bool(self._hwnd and is_foreground(self._hwnd))

    def _decide(self, kind, fn):
        """上升沿决策(每个提示只调一次):重现免 OCR;其余读一次名字按白/黑名单判。"""
        if kind == "chongxian" and not self.rec.whitelist:
            return (True, "重现", "chongxian")
        return self.rec.judge(kind, self.rec.read_name(fn))

    def run(self) -> None:
        self.stop_flag = False
        self.picked = 0
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        self._hwnd = hwnd
        if not self.rec.ready:
            self.log("自动采集未标定:缺 F 键帽/图标模板(pick_f / icon_pick / icon_chongxian);现在空转、不按键")
            dev_log("采集启动失败:识别模板未就绪")
            return
        if not is_admin():
            self.log("⚠ 非管理员运行!按 F 会被提权游戏拦截(识别得到却采不到)→ 请以管理员重启本程序")
        self.log(f"自动采集已启动(图标识别即时按 F,只在新提示出现时读一次名字核对;"
                 f"碰撞名单 {len(self.rec.blacklist)} 条 / 白名单 {len(self.rec.whitelist)} 条;"
                 "NPC/商店/对话不动;仅游戏前台;F12 急停)")

        last_tick = 0.0
        prompt_active = False      # 当前是否处于"一段提示"中(已决策过)
        decided_press = False      # 该段决策结果:采(True)/跳(False);跳→之后不再 OCR 空转
        skip_logged = False        # 该段"跳过"是否已记一次日志(避免刷屏)
        press_round = 0            # 该段已按 F 次数(上限 MAX_PRESS,防采不掉的手型无限连按)
        notext_round = 0           # 该段"名字没读清"已重读次数(上限 NOTEXT_MAX)
        rechecking = False         # 当前是否处于"名字没读清、快速重读"状态(不按 F,只重 OCR)
        last_recheck = 0.0         # 上次"没读清重读"的时刻(节奏 NOTEXT_GAP)
        last_press = 0.0           # 上次按 F / 复核的时刻(连按节奏 RETRY_GAP)
        last_seen = 0.0            # 最近一次见到可动作提示(去抖,防闪烁误复位)
        last_fg_warn = 0.0         # 「游戏不在前台」提示限频
        last_prompt = time.time()  # 最近一次屏幕上有 F 提示(含 NPC/商店等一切提示;空闲降频用,启动先满速)
        text = ""
        try:
            with GameCapture(hwnd) as cap:
                self.log("画面捕获已就绪(GDI BitBlt,无黄框、无光标闪烁)")
                while not self.stop_flag:
                    if self.paused or not is_foreground(hwnd):
                        prompt_active = False        # 暂停/切走 → 复位,回来当新提示
                        if not self.paused and time.time() - last_fg_warn > 3.0:
                            last_fg_warn = time.time()
                            self.log("⏸ 游戏不在最前台 → 已暂停")
                        time.sleep(0.2)
                        continue
                    now = time.time()
                    # 空闲降频:附近没有任何可交互提示时慢扫(跑图/战斗把 CPU/GDI 让给游戏),见提示即回满速
                    interval = (self.DETECT_INTERVAL if now - last_prompt < self.IDLE_AFTER
                                else self.IDLE_INTERVAL)
                    if now - last_tick < interval:
                        time.sleep(0.01)
                        continue
                    last_tick = now
                    f = cap.grab_region_canvas(GATHER_REGION)   # **只截中部一小块**(~7ms,整屏 ~60ms)→ 提速 5×+
                    if f is None:
                        continue
                    kind, fn = self.rec.classify(f)          # 快路:无 OCR
                    if kind != "none":
                        last_prompt = now                    # 任何 F 提示在场(含 NPC/商店)都保持满速
                    # 可动作 = 手型/重现;"别的图标"仅当配了白名单才需读字核对(否则直接忽略,不 OCR)
                    actionable = kind in ("pick", "chongxian") or (kind == "other" and self.rec.whitelist)
                    if actionable:
                        last_seen = now
                        rising = not prompt_active
                        # 触发时机:上升沿(新提示) 或 已决"采"且到了连按节奏(收掉最近的、提示跳到下一个 → 接着收)。
                        # 已决"跳"的提示就站着不动、不再 OCR(站铸星/渡石前不空耗),靠提示消失复位。
                        due = (rising
                               or (decided_press and press_round < self.MAX_PRESS
                                   and now - last_press >= self.RETRY_GAP)
                               or (rechecking and notext_round < self.NOTEXT_MAX
                                   and now - last_recheck >= self.NOTEXT_GAP))
                        if due:
                            press, text, reason = self._decide(kind, fn)   # OCR 复核一次(连按期同屏混入黑名单会被拦下)
                            if press:
                                prompt_active, rechecking = True, False
                                decided_press, skip_logged, notext_round = True, False, 0
                                self._press_f()
                                self.picked += 1
                                self.on_count(self.picked)
                                self.log(f"采集:{text}  #{self.picked}")
                                press_round = press_round + 1 if not rising else 1
                                last_press = now
                            elif reason == "no-text" and notext_round < self.NOTEXT_MAX:
                                # 名字没读清 → **本帧绝不按 F**(可能是没读清的碰撞名单,如语印/渡石);
                                # 隔很短的 NOTEXT_GAP 快速重读:读到材料名才采、读到黑名单则跳;多次仍读不出 → 转跳过停手。
                                prompt_active, rechecking = True, True
                                decided_press = False
                                notext_round += 1
                                last_recheck = now
                            else:
                                prompt_active, rechecking = True, False
                                decided_press = False        # 读到黑名单/别的图标/多次读不出 → 停手,直到提示消失复位
                                if not skip_logged:
                                    skip_logged = True
                                    if reason.startswith("skip"):
                                        self.log(f"跳过碰撞名单「{text}」")
                    elif prompt_active and now - last_seen >= self.ABSENT_RESET:
                        prompt_active, press_round, decided_press, skip_logged = False, 0, False, False
                        notext_round, rechecking = 0, False
                    time.sleep(0.01)
        finally:
            release_known_keys(self.log)
        self.log(f"自动采集结束,共采 {self.picked} 处")


if __name__ == "__main__":
    GatherPicker().run()
