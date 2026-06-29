"""HOKWord 实时剧情跳过引擎 v3(配合 recognizer v2 正向门 + 两步识别)。

识别交给 StoryRecognizer:classify(纯模板,快)给粗状态;门内非可跳过时本文件**限频**调
read_options(OCR)查选项。本文件只做"状态 → 动作"。

**三条核心保证(都来自用户实测反馈)**:
  1) 只在 positively 在剧情(右上 [F9]抓拍 门成立)或确认框时才动鼠标 → 切活动/日常面板等菜单一律
     idle,**光标纹丝不动**(不再有任何"黑屏点击 / 鼠标微动"在非剧情时移动光标)。
  2) **剧情结束 / 段间黑屏立即停手**:黑屏/过场/回到游戏都判 idle 不动作 → 杜绝"过完剧情误点=攻击"。
     停手后每帧用 F9 门快速重判:进入下一段剧情就继续,回到游戏就一直不动。
  3) **不可跳过对话快速连点推进**:story 态下按很短固定间隔点中性点连推(无随机延迟),选项检查用 OCR
     但**限频**(每 OPT_CHECK 秒一次),故连点不被 OCR 拖慢;可跳过段优先 ESC(最快)。

点击一律**直接移动到目标 + 立即点一下**(不走随机弧线、不抖动、不拖延 → 无闪烁)。中性点固定,连点
不再移动光标。复用 recorder 的窗口/前台/截图;仅游戏前台时动作;F12 全局急停。
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import find_game_hwnd, is_foreground  # noqa: E402
from capture import GameCapture  # noqa: E402
from story.recognizer import StoryRecognizer  # noqa: E402
from config import cfg  # noqa: E402
from runtime_guard import dev_log, release_known_keys, safe_click_norm, safe_press_key  # noqa: E402

NEUTRAL_PT = (0.5, 0.92)       # 推进点击点 = 对话框底部居中的「继续」指示符(向下箭头︶/三个点)位置:
                               # 看真实帧(sessions/.../000500 箭头、000510 三点)——点这里才会"立即进入下一句";
                               # 箭头在=点了就翻页,三点(配音中)=点了无效但无害 → 快速连点≈以游戏允许的最快速度推进


class StorySkipper:
    TICK = 0.04             # 主循环 tick(classify 纯模板很快,可高频)
    CLICK_DELAY = (0.1, 0.3)  # 每次鼠标点击的随机延迟(秒);兼作 story 连点的随机间隔(拟人,不固定)
    CONFIRM_GAP = 0.8       # 两次点确认框「跳过」最小间隔
    OPT_CHECK = 0.3         # story 态 OCR(控制条「跳过」兜底 + 选项)的限频间隔
    SKIP_HOLD = 1.0         # 见到"可跳过"信号后这么久内只走 ESC、绝不点击推进(修复"跳过条已出却还点两三下")
    ESC_PENDING_S = 1.3     # 按 ESC 后等确认框最长时间;超时未见 → 放弃
    POST_SKIP_BLOCK = 1.2   # 成功跳过后这么久内不按 ESC(跨过淡出残留)
    ABORT_BLOCK = 2.0       # 「ESC 无确认框」放弃后这么久内不按 ESC
    VK_ESC = 0x1B

    def __init__(self, log=print, on_count=lambda n: None) -> None:
        self.rec = StoryRecognizer()
        self.log = log
        self.on_count = on_count
        self.stop_flag = False
        self.paused = False
        self.skipped = 0
        self._hwnd = None

    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = on

    def _press_esc(self) -> bool:
        return safe_press_key(self.VK_ESC, self._stopped, self._foreground, self.log, 0.05)

    def _click_norm(self, hwnd, pt) -> None:
        """点客户区归一化坐标:**直接定位 + 立即点击**(不走弧线/不抖动 → 无闪烁、不拖延)。"""
        safe_click_norm(hwnd, pt, self._stopped, self._foreground, self.log, 0.02)

    def _stopped(self) -> bool:
        return bool(self.stop_flag)

    def _foreground(self) -> bool:
        return bool(self._hwnd and is_foreground(self._hwnd))

    def run(self, nudge: bool = False) -> None:        # nudge 兼容旧签名,已弃用(不再微动)
        self.stop_flag = False
        self.skipped = 0
        _ = cfg.timing_jitter()
        hwnd = find_game_hwnd()
        if not hwnd:
            self.log("未找到游戏窗口『王者荣耀世界』,请先运行游戏")
            return
        self._hwnd = hwnd
        if not self.rec.ready:
            self.log("剧情识别未标定:缺 story/templates/raw 模板(kc_f9/kc_esc/confirm_skip)")
            dev_log("剧情启动失败:识别模板未就绪")
            return
        self.log("实时检测已启动(只在剧情里动作:菜单/游戏/黑屏过场一律不动;仅游戏前台;F12 急停)")
        dbg = self._open_debug()

        esc_pending = False
        esc_t = 0.0
        block_esc_until = 0.0
        last_skip = 0.0
        last_skip_seen = -99.0            # 上次见到"可跳过"信号(模板 skip 或 OCR 读到「跳过」)
        next_advance = 0.0
        last_opt_check = 0.0
        last_confirm_check = 0.0          # 确认框标题 OCR 复核的限频(防菜单金按钮每帧 OCR)
        bar_state = "none"                # 限频 OCR 精判结果:skip / story / none(见 recognizer.read_bar)
        opt_mode, opt_pt = "none", None   # story 态的选项判定(限频刷新):none/choice/hold
        last_dbg = ""
        last_log = ""
        last_fg_warn = 0.0                 # 「游戏不在前台」提示限频
        gone_since = 0.0                   # 游戏窗口彻底消失起始时刻(用户退出游戏 → 停止)
        try:
            with GameCapture(hwnd) as cap:
                self.log("画面捕获已就绪(GDI BitBlt,无黄框、无光标闪烁)")
                while not self.stop_flag:
                    if self.paused or not is_foreground(hwnd):   # 安全:只在游戏前台时动作
                        # 用户退出游戏(窗口彻底没了)→ 停止;只是没焦点/最小化 → 暂停。给 5s 宽限防加载瞬断。
                        if not self.paused and find_game_hwnd() is None:
                            if not gone_since:
                                gone_since = time.time()
                            elif time.time() - gone_since > 5.0:
                                self.log("游戏已退出 → 停止实时检测")
                                break
                        else:
                            gone_since = 0.0
                        if not self.paused and time.time() - last_fg_warn > 3.0:
                            last_fg_warn = time.time()
                            self.log("⏸ 游戏不在最前台 → 已暂停")
                        time.sleep(0.2)
                        continue
                    gone_since = 0.0
                    now = time.time()
                    f = cap.grab()
                    if f is None:
                        time.sleep(self.TICK)
                        continue

                    state, pt = self.rec.classify(f)          # confirm / gate(也许在剧情) / idle —— 纯模板,快
                    if state != last_dbg:
                        self._dbg(dbg, now, state)
                        last_dbg = state
                    if state != "gate":
                        bar_state, opt_mode, opt_pt = "none", "none", None   # 离开剧情 → 复位精判
                    # ESC 等待超时 → 放弃冷却(任何状态下都判,杜绝连按 ESC)
                    if esc_pending and now - esc_t > self.ESC_PENDING_S:
                        esc_pending = False
                        block_esc_until = now + self.ABORT_BLOCK
                        self._dbg(dbg, now, ">> ESC 后未见确认框(超时放弃)")
                        self.log("ESC 后未见确认框,暂停(剧情可能已结束)")

                    # gate 态:限频 OCR 精判(背景无关)→ skip / story / none;none=模板假阳,实际不在剧情
                    if state == "gate" and now - last_opt_check >= self.OPT_CHECK:
                        last_opt_check = now
                        bar_state = self.rec.read_bar(f)
                        if bar_state == "skip":
                            last_skip_seen = now
                            self._dbg(dbg, now, ">> read_bar=SKIP")
                        elif bar_state == "story":
                            opt_mode, opt_pt = self.rec.read_options(f)
                    skip_active = now - last_skip_seen < self.SKIP_HOLD

                    if state == "confirm":
                        # 金「跳过」按钮模板易在菜单其它金按钮上误配 → 限频 OCR 复核标题含「本段」才点;
                        # confirm 帧**永不**落到下面的 ESC/advance(即便 skip_active),防误按 ESC 关掉确认框。
                        if pt and now - last_confirm_check >= 0.4:
                            last_confirm_check = now
                            isd = self.rec.is_skip_dialog(f)
                            self._dbg(dbg, now, f">> CONFIRM态 is_skip_dialog={isd} gap_ok={now-last_skip>self.CONFIRM_GAP}")
                            if now - last_skip > self.CONFIRM_GAP and isd:
                                time.sleep(random.uniform(*self.CLICK_DELAY))   # 点击随机延迟
                                self._click_norm(hwnd, pt)
                                self.skipped += 1
                                self.on_count(self.skipped)
                                self._dbg(dbg, now, ">> CLICK confirm 完成跳过")
                                self.log(f"✓ 跳过剧情(确认「跳过」)#{self.skipped}")
                                last_skip = now
                                esc_pending = False
                                block_esc_until = now + self.POST_SKIP_BLOCK
                                time.sleep(0.3)

                    elif state == "idle":
                        pass   # 菜单/游戏/黑屏过场 → 光标不动、不误点(即便刚跳过也不在这里 ESC)

                    elif skip_active:
                        # 可跳过段(OCR 读到「跳过」)→ 只走 ESC 调确认框,**绝不点击推进**
                        # (修复"跳过条已出却还点两三下才跳")
                        if not esc_pending and now > block_esc_until:
                            sent = self._press_esc()
                            self._dbg(dbg, now, f">> PRESS ESC sent={sent}")
                            self.log("检测到可跳过剧情 → ESC,等待确认框")
                            esc_pending, esc_t = True, now

                    elif bar_state == "story":
                        # OCR 确认在剧情、非可跳过:旁白/不可跳过 → 点箭头处快速推进;真选项 → 点该项;再见/退出 → hold。
                        if opt_mode == "hold":
                            if last_log != "hold":
                                self.log("对话选项含「再见/退出」→ 交给你手动选择,脚本不点")
                                last_log = "hold"
                        elif now >= next_advance:
                            target = opt_pt if (opt_mode == "choice" and opt_pt) else NEUTRAL_PT
                            self._click_norm(hwnd, target)
                            next_advance = now + random.uniform(*self.CLICK_DELAY)   # 随机间隔(拟人)
                            tag = "对话选项 → 点第一项" if opt_mode == "choice" else "不可跳过剧情 → 点击推进"
                            if last_log != tag:
                                self.log(tag)
                                last_log = tag

                    # gate 但 bar_state=='none'(模板假阳,OCR 没读到剧情字)→ 不动作
                    time.sleep(self.TICK)
        finally:
            release_known_keys(self.log)
            self._close_debug(dbg)
        self.log(f"实时检测结束,共跳过 {self.skipped} 段")

    # ---- 调试日志(状态序列)----
    def _open_debug(self):
        try:
            d = HERE.parent / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            fp = open(d / "_story_debug.log", "a", encoding="utf-8")
            fp.write(f"\n==== run {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
            fp.flush()
            return fp
        except Exception as exc:
            dev_log("剧情调试日志打开失败", exc)
            return None

    def _dbg(self, fp, now, state) -> None:
        if fp is None:
            return
        try:
            fp.write(f"{time.strftime('%H:%M:%S')}  {state}\n")
            fp.flush()
        except Exception as exc:
            dev_log("剧情调试日志写入失败", exc)

    def _close_debug(self, fp) -> None:
        try:
            fp and fp.close()
        except Exception as exc:
            dev_log("剧情调试日志关闭失败", exc)


if __name__ == "__main__":
    StorySkipper().run()
