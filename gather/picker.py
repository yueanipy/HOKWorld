'HOKWord 实时采集引擎:跑图经过材料 → 识别 F 提示 → 按 F 采集。'
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from winenv import find_game_hwnd, is_admin, is_foreground  # noqa: E402
from capture_broker import subscribe_capture  # noqa: E402
from gather.recognizer import GATHER_REGION, GatherRecognizer  # noqa: E402
from runtime_guard import dev_log, release_known_keys, safe_press_key  # noqa: E402

VK_F = 0x46


class GatherPicker:
    DETECT_INTERVAL = 0.02     
    IDLE_INTERVAL = 0.05       
    IDLE_AFTER = 180.0         
    RETRY_GAP = 0.25          
    MAX_PRESS = 20            
    ABSENT_RESET = 0.3       
    NOTEXT_MAX = 4           
    NOTEXT_GAP = 0.02        

    def __init__(self, log=print, on_count=lambda n: None, on_foreground=lambda active: None) -> None:
        self.rec = GatherRecognizer()
        self.log = log
        self.on_count = on_count
        self.on_foreground = on_foreground
        self.stop_flag = False
        self.paused = False
        self.picked = 0
        self._hwnd = None

    def stop(self) -> None:
        self.stop_flag = True
        release_known_keys(self.log)

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        if self.paused:
            release_known_keys(self.log)

    def _press_f(self) -> bool:
        return safe_press_key(VK_F, self._stopped, self._foreground, self.log, 0.05)

    def _stopped(self) -> bool:
        return bool(self.stop_flag or self.paused)

    def _foreground(self) -> bool:
        return bool(self._hwnd and is_foreground(self._hwnd))

    def _decide(self, kind, fn):
        '上升沿决策(每个提示只调一次):重现免 OCR;其余读一次名字按白/黑名单判。'
        if kind == "chongxian" and not self.rec.whitelist:
            return (True, "重现", "chongxian")
        return self.rec.judge(kind, self.rec.read_name(fn))

    def run(self) -> None:
        
        if self.stop_flag:
            return
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

        prompt_active = False      
        decided_press = False      
        skip_logged = False        
        press_round = 0            
        notext_round = 0           
        rechecking = False         
        last_recheck = 0.0         
        last_press = 0.0           
        last_seen = 0.0            
        last_foreground = None     
        
        last_prompt = time.time() - self.IDLE_AFTER
        text = ""
        try:
            with subscribe_capture(hwnd, "gather", [GATHER_REGION], self.IDLE_INTERVAL) as frames:
                self.log("共享画面捕获已就绪(CaptureBroker,与剧情/月卡复用同一 GDI 帧)")
                while not self.stop_flag:
                    foreground = is_foreground(hwnd)
                    if foreground != last_foreground:
                        previous = last_foreground
                        last_foreground = foreground
                        self.on_foreground(foreground)
                        if not foreground:
                            self.log("⏸ 游戏不在最前台 → 已暂停")
                        elif previous is False:
                            self.log("游戏已回到前台 → 自动继续")
                    if self.paused or not foreground:
                        frames.set_enabled(False)
                        prompt_active = False        
                        time.sleep(0.2)
                        continue
                    now = time.time()
                    
                    interval = (self.DETECT_INTERVAL if now - last_prompt < self.IDLE_AFTER
                                else self.IDLE_INTERVAL)
                    snapshot = frames.get_frame(interval, [GATHER_REGION], timeout=max(0.5, interval * 3))
                    f = snapshot.frame if snapshot else None
                    if f is None:
                        continue
                    kind, fn = self.rec.classify(f)          
                    if kind != "none":
                        last_prompt = now                    
                    
                    actionable = kind in ("pick", "chongxian") or (kind == "other" and self.rec.whitelist)
                    if actionable:
                        last_seen = now
                        rising = not prompt_active
                        
                        
                        due = (rising
                               or (decided_press and press_round < self.MAX_PRESS
                                   and now - last_press >= self.RETRY_GAP)
                               or (rechecking and notext_round < self.NOTEXT_MAX
                                   and now - last_recheck >= self.NOTEXT_GAP))
                        if due:
                            press, text, reason = self._decide(kind, fn)   
                            if press:
                                if not self._press_f():
                                    continue
                                prompt_active, rechecking = True, False
                                decided_press, skip_logged, notext_round = True, False, 0
                                self.picked += 1
                                self.on_count(self.picked)
                                self.log(f"采集:{text}  #{self.picked}")
                                press_round = press_round + 1 if not rising else 1
                                last_press = now
                            elif reason == "no-text" and notext_round < self.NOTEXT_MAX:
                                
                                
                                prompt_active, rechecking = True, True
                                decided_press = False
                                notext_round += 1
                                last_recheck = now
                            else:
                                prompt_active, rechecking = True, False
                                decided_press = False        
                                if not skip_logged:
                                    skip_logged = True
                                    if reason.startswith("skip"):
                                        self.log(f"跳过碰撞名单「{text}」")
                    elif prompt_active and now - last_seen >= self.ABSENT_RESET:
                        prompt_active, press_round, decided_press, skip_logged = False, 0, False, False
                        notext_round, rechecking = 0, False
        finally:
            release_known_keys(self.log)
        self.log(f"自动采集结束,共采 {self.picked} 处")


if __name__ == "__main__":
    GatherPicker().run()
