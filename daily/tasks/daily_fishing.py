'每日钓鱼：传送到云根镇云舟栈，前往岸边并调用钓鱼状态机钓三条。'
from __future__ import annotations

import threading
import time
from pathlib import Path

import cv2
import numpy as np

import daily.recognizer as rec
from daily.base import DailyTask, TaskResult
from world_map import teleport_to
from fishing.fisher import FishingBot
from fishing.matcher import FishingRecognizer
from runtime_guard import dev_log


class DailyFishingTask(DailyTask):
    task_id = "daily_fishing"
    name = "每日钓鱼(3次)"

    ROI_ALL = (0.0, 0.0, 1.0, 1.0)
    ROI_TOOL_PANEL = (0.045, 0.18, 0.38, 0.90)
    ROD_TAB = (0.302, 0.462)
    ROD_COLS = (0.145, 0.192, 0.239, 0.286, 0.333)
    ROD_ROWS = (0.568, 0.656, 0.744)
    QUICKBAR_ROD_TEMPLATES = {
        "blue": Path(__file__).resolve().parents[1] / "templates" / "quickbar_blue_rod.png",
        "green": Path(__file__).resolve().parents[1] / "templates" / "quickbar_green_rod.png",
    }
    _quickbar_rod_templates = None
    SHORE_STRAFE_S = 0.8
    SHORE_TURN_PX = 300      
    SHORE_MIN_WALK_S = 5.0   
    SHORE_MAX_WALK_S = 10.0  
    SHORE_MID_RATIO = 0.60
    SHORE_LOW_RATIO = 0.30
    SHORE_CENTER_RATIO = 0.62
    SHORE_FRONT_RATIO = 0.35
    HUD_KEYWORDS = ("抓拍", "好友", "切换", "输入", "高处")
    FISHING_ACTIVE_STATES = frozenset(("FISHING_READY", "WAITING_FOR_BITE", "HOOK_PROMPT"))

    def _navigate_to_yungen_pier(self) -> bool:
        '调用公共全地图传送到云根镇云舟栈，不保留任务私有地图逻辑。'
        stable = 0

        def arrived(frame) -> bool:
            nonlocal stable
            ready = self._world_hud_ready(frame)
            stable = stable + 1 if ready else 0
            return stable >= 2

        self.ctx.log(f"{self.name}:调用全地图传送点“云根镇云舟栈”")
        ok = teleport_to(
            self.ctx, "云根镇云舟栈", timeout=45.0,
            arrival_predicate=arrived,
        )
        if ok:
            self.ctx.sleep(1.0)
        return bool(ok)

    @classmethod
    def _world_hud_diagnostics(cls, frame) -> tuple[tuple[str, ...], bool]:
        '返回普通世界 HUD 命中词和是否仍被地图/传送详情遮挡。'
        text = rec.ocr_text(frame, cls.ROI_ALL)
        hits = tuple(k for k in cls.HUD_KEYWORDS if k in text)
        blocked = "云根镇云舟栈" in text or rec.in_world_map(frame)
        return hits, blocked

    @classmethod
    def _world_hud_ready(cls, frame) -> bool:
        '传送完成正向门：至少两个普通世界 HUD 词，且地图/传送详情均已消失。'
        hits, blocked = cls._world_hud_diagnostics(frame)
        return len(hits) >= 2 and not blocked

    @staticmethod
    def _water_ratios(frame) -> tuple[float, float, float, float]:
        f = rec.normalize(frame)
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]

        def ratio(box):
            x0, y0, x1, y1 = box
            sub = hsv[int(y0*h):int(y1*h), int(x0*w):int(x1*w)]
            if sub.size == 0:
                return 0.0
            mask = ((sub[:, :, 0] >= 82) & (sub[:, :, 0] <= 112) &
                    (sub[:, :, 1] >= 45) & (sub[:, :, 2] >= 45))
            return float(mask.mean())

        
        
        front = (ratio((0.15, 0.52, 0.43, 0.76))
                 + ratio((0.57, 0.52, 0.85, 0.76))) * 0.5
        return (ratio((0.15, 0.25, 0.85, 0.62)),
                ratio((0.15, 0.42, 0.85, 0.68)),
                ratio((0.30, 0.25, 0.70, 0.62)),
                front)

    def _walk_to_shore(self) -> bool:
        ctx = self.ctx
        if not ctx.walk("d", self.SHORE_STRAFE_S):
            return False
        ctx.sleep(0.20)
        ctx.log(f"{self.name}:D {self.SHORE_STRAFE_S:.1f}秒完成，"
                f"开始按住 W 并同步右转{self.SHORE_TURN_PX}px")
        start = ctx.logical_time()
        streak = 0
        camera_turned = False
        while ctx.logical_time() - start < self.SHORE_MAX_WALK_S and not ctx.should_stop():
            if ctx.paused:
                time.sleep(0.08)
                continue
            if not ctx.foreground():
                ctx.log(f"{self.name}:寻岸时游戏失去前台，已立即抬起 W 并停止路线")
                return False
            with ctx.hold("w") as held:
                if not held:
                    return False
                if not camera_turned:
                    
                    if not ctx.drag_camera(self.SHORE_TURN_PX, steps=20):
                        ctx.log(f"{self.name}:W 行进中的右转视角失败，已抬起 W")
                        return False
                    camera_turned = True
                while (ctx.logical_time() - start < self.SHORE_MAX_WALK_S
                       and not ctx.should_stop()):
                    if not ctx.action_ready():
                        break  
                    frame = ctx.grab_nowait()
                    if frame is None:
                        time.sleep(0.03)
                        continue
                    mid, low, center, front = self._water_ratios(frame)
                    elapsed = ctx.logical_time() - start
                    
                    
                    at_edge = (elapsed >= self.SHORE_MIN_WALK_S
                               and mid >= self.SHORE_MID_RATIO
                               and low >= self.SHORE_LOW_RATIO
                               and center >= self.SHORE_CENTER_RATIO
                               and front >= self.SHORE_FRONT_RATIO)
                    streak = streak + 1 if at_edge else 0
                    dev_log(f"[daily fishing] 寻岸 water_mid={mid:.3f} water_low={low:.3f} "
                            f"water_center={center:.3f} water_front={front:.3f} "
                            f"elapsed={elapsed:.1f}/{self.SHORE_MIN_WALK_S:.0f}s streak={streak}")
                    if streak >= 2:
                        break
                    time.sleep(0.10)
            if streak >= 2:
                break
            if not ctx.paused and not ctx.action_ready():
                ctx.log(f"{self.name}:寻岸输入权限失效，已停止路线")
                return False
        if streak < 2:
            ctx.log(f"{self.name}:前进 {self.SHORE_MAX_WALK_S:.0f} 秒仍未确认到达水陆交界")
            return False
        ctx.log(f"{self.name}:已到达水陆交界")
        
        ctx.sleep(0.85)
        return True

    @staticmethod
    def _fishing_ready(frame, recognizer: FishingRecognizer) -> bool:
        state, _ = recognizer.classify(frame)
        return state in ("FISHING_READY", "WAITING_FOR_BITE")

    @staticmethod
    def _fishing_state(frame, recognizer: FishingRecognizer) -> str:
        state, _ = recognizer.classify(frame)
        return state

    @staticmethod
    def _rod_card_candidates(frame):
        '读取鱼竿卡片的稀有度与右上角选中勾；空格和紫色物品不进入候选。'
        f = rec.normalize(frame)
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]
        blue, green = [], []
        for row_y in DailyFishingTask.ROD_ROWS:
            for col_x in DailyFishingTask.ROD_COLS:
                strip = hsv[int((row_y + 0.031)*h):int((row_y + 0.041)*h),
                            int((col_x - 0.017)*w):int((col_x + 0.017)*w)]
                if strip.size == 0:
                    continue
                interior = f[int((row_y - 0.028)*h):int((row_y + 0.025)*h),
                             int((col_x - 0.014)*w):int((col_x + 0.014)*w)]
                if interior.size == 0 or float(cv2.cvtColor(interior, cv2.COLOR_BGR2GRAY).std()) < 8.0:
                    continue  
                pixels = strip.reshape(-1, 3)
                vivid = pixels[(pixels[:, 1] >= 65) & (pixels[:, 2] >= 80)]
                if len(vivid) < 0.08 * len(pixels):
                    continue
                hue = float(np.median(vivid[:, 0]))
                
                mark = hsv[int((row_y - 0.030)*h):int((row_y - 0.007)*h),
                           int((col_x + 0.006)*w):int((col_x + 0.019)*w)]
                selected = bool(mark.size and np.mean(
                    (mark[:, :, 1] < 45) & (mark[:, :, 2] > 220)) >= 0.25)
                card = {"pt": (col_x, row_y), "selected": selected, "hue": hue}
                if 100 <= hue <= 118:
                    card["rarity"] = "blue"
                    blue.append(card)
                elif 72 <= hue < 100:
                    card["rarity"] = "green"
                    green.append(card)
        return blue, green

    @classmethod
    def _quickbar_has_rod(cls, frame) -> tuple[bool, dict]:
        '识别快捷栏第 4 格的蓝色或绿色鱼竿，命中后禁止再长按 4。'
        empty = {"rarity": "none", "score": 0.0, "gray": 0.0, "edge": 0.0,
                 "blue_score": 0.0, "green_score": 0.0}
        if frame is None:
            return False, empty
        if cls._quickbar_rod_templates is None:
            cls._quickbar_rod_templates = {
                rarity: cv2.imread(str(path))
                for rarity, path in cls.QUICKBAR_ROD_TEMPLATES.items()
            }
        templates = {
            rarity: template
            for rarity, template in cls._quickbar_rod_templates.items()
            if template is not None and template.size > 0
        }
        if not templates:
            return False, empty

        f = rec.normalize(frame)
        h, w = f.shape[:2]
        search = f[max(0, h - 140):max(1, h - 15), int(0.325*w):int(0.390*w)]
        if search.size == 0:
            return False, empty
        search_gray = cv2.GaussianBlur(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        search_edge = cv2.Canny(search_gray, 45, 120)
        best = dict(empty)
        per_rarity = {"blue": 0.0, "green": 0.0}
        for rarity, template in templates.items():
            rarity_best = -1.0
            for scale in (0.88, 0.94, 1.0, 1.06, 1.12):
                candidate = cv2.resize(
                    template, None, fx=scale, fy=scale,
                    interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
                if candidate.shape[0] > search.shape[0] or candidate.shape[1] > search.shape[1]:
                    continue
                gray = cv2.GaussianBlur(cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY), (3, 3), 0)
                edge = cv2.Canny(gray, 45, 120)
                gray_score = float(cv2.minMaxLoc(
                    cv2.matchTemplate(search_gray, gray, cv2.TM_CCOEFF_NORMED))[1])
                edge_score = float(cv2.minMaxLoc(
                    cv2.matchTemplate(search_edge, edge, cv2.TM_CCOEFF_NORMED))[1])
                score = 0.55 * gray_score + 0.45 * edge_score
                rarity_best = max(rarity_best, score)
                if score > best["score"]:
                    best.update({"rarity": rarity, "score": score,
                                 "gray": gray_score, "edge": edge_score})
            per_rarity[rarity] = max(0.0, rarity_best)
        best["blue_score"] = per_rarity["blue"]
        best["green_score"] = per_rarity["green"]
        return best["score"] >= 0.52, best

    def _equip_rod_if_needed(self, recognizer: FishingRecognizer) -> str | None:
        '返回 ready/waiting/equipped；不通过短按 4 猜道具，避免误用消耗品或提前抛竿。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is not None:
            state = self._fishing_state(frame, recognizer)
            if state == "FISHING_READY":
                return "ready"
            if state == "WAITING_FOR_BITE":
                return "waiting"

        panel = frame is not None and self._tool_panel_open(frame)
        if not panel:
            has_rod, quick_scores = self._quickbar_has_rod(frame)
            dev_log(f"[daily fishing] 快捷栏鱼竿识别 hit={has_rod} "
                    f"scores={quick_scores}")
            if has_rod:
                rarity = "蓝色" if quick_scores["rarity"] == "blue" else "绿色"
                ctx.log(f"{self.name}:快捷栏第 4 格已是{rarity}鱼竿，跳过长按面板")
                return "equipped"
            if not ctx.press("4", hold_s=1.15):
                dev_log("[daily fishing] 长按 4 打开随身道具被输入门禁拒绝")
                return None
            panel = ctx.wait_until(self._tool_panel_open,
                                   timeout=4.0, interval=0.25, desc="打开随身道具")
        if not panel:
            dev_log("[daily fishing] 长按 4 后未确认随身道具面板")
            return None
        if not ctx.click(self.ROD_TAB):
            dev_log("[daily fishing] 点击鱼竿分类失败")
            return None
        ctx.sleep(0.6)
        frame = ctx.grab()
        blue, green = self._rod_card_candidates(frame) if frame is not None else ([], [])
        
        candidates = blue if blue else green
        dev_log(f"[daily fishing] 鱼竿识别 blue={blue} green={green} "
                f"chosen={'blue' if blue else 'green' if green else 'none'}")
        if not candidates:
            ctx.log(f"{self.name}:鱼竿分类中未找到蓝色或绿色道具")
            ctx.press("esc")
            return None

        
        for card in candidates:
            chosen = card["pt"]
            rarity = "蓝色" if card["rarity"] == "blue" else "绿色"
            if card["selected"]:
                ctx.log(f"{self.name}:{rarity}鱼竿已装备，关闭随身道具")
                if not ctx.press("esc"):
                    dev_log("[daily fishing] 已装备鱼竿后 ESC 关闭面板被拒绝")
                    return None
                closed = ctx.wait_until(lambda f: not self._tool_panel_open(f),
                                        timeout=2.0, interval=0.20)
                dev_log(f"[daily fishing] 已装备{rarity}鱼竿，面板关闭确认={bool(closed)}")
                return "equipped" if closed else None
            if not ctx.click(chosen):
                return None
            closed = ctx.wait_until(lambda f: not self._tool_panel_open(f),
                                    timeout=1.6, interval=0.20)
            if closed:
                ctx.sleep(0.45)
                ctx.log(f"{self.name}:已选择{rarity}鱼竿")
                return "equipped"
            dev_log(f"[daily fishing] {rarity}鱼竿候选 {chosen} 未关闭面板，按不可选处理")
        ctx.log(f"{self.name}:{'蓝色' if blue else '绿色'}鱼竿候选不可选择")
        ctx.press("esc")
        return None

    @classmethod
    def _tool_panel_open(cls, frame) -> bool:
        '只 OCR 左侧面板，避免整屏 OCR 拖慢“关闭面板”状态翻转确认。'
        return frame is not None and "随身道具" in rec.ocr_text(frame, cls.ROI_TOOL_PANEL)

    def _activate_equipped_rod(self, recognizer: FishingRecognizer,
                               *, verified_equipped: bool = False) -> str | None:
        '短按 4 激活鱼竿；每次超时后安全复核，最多尝试三次。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None:
            return None
        state, _ = recognizer.classify(frame)
        if state == "FISHING_READY":
            return "ready"
        if state == "WAITING_FOR_BITE":
            return "waiting"
        if self._tool_panel_open(frame):
            ctx.log(f"{self.name}:鱼竿面板仍在，先关闭后再激活")
            if not ctx.press("esc"):
                return None
            closed = ctx.wait_until(lambda f: not self._tool_panel_open(f),
                                    timeout=2.0, interval=0.18)
            if not closed:
                dev_log("[daily fishing] 激活前关闭鱼竿面板失败")
                return None
        for attempt in (1, 2, 3):
            
            if not ctx.press("4", hold_s=0.08):
                dev_log(f"[daily fishing] 短按 4 激活鱼竿被拒绝 attempt={attempt}")
                return None

            observed = {"state": "UNKNOWN", "scores": {}}

            def active(f):
                st, sc = recognizer.classify(f)
                observed["state"], observed["scores"] = st, sc
                return st in ("FISHING_READY", "WAITING_FOR_BITE")

            
            ok = ctx.wait_until(active, timeout=5.0, interval=0.16,
                                desc=f"鱼竿进入钓鱼态({attempt}/3)")
            dev_log(f"[daily fishing] 短按 4 激活 attempt={attempt} ok={bool(ok)} "
                    f"state={observed['state']} scores={observed['scores']}")
            if ok:
                return "waiting" if observed["state"] == "WAITING_FOR_BITE" else "ready"
            if attempt == 3:
                return None

            
            
            
            frame = ctx.grab()
            world_ready = frame is not None and self._world_hud_ready(frame)
            has_rod, quick_scores = self._quickbar_has_rod(frame)
            dev_log(f"[daily fishing] 第 {attempt} 次短按 4 未生效复核 world={world_ready} "
                    f"rod={has_rod} verified_equipped={verified_equipped} "
                    f"scores={quick_scores}")
            if not world_ready:
                return None
            if not has_rod and not verified_equipped:
                return None
            if not has_rod:
                dev_log(f"[daily fishing] 快捷栏鱼竿模板假阴候选，沿用本轮已装备强证据 "
                        f"attempt={attempt} score={quick_scores.get('score', 0.0):.3f}")
            ctx.log(f"{self.name}:第 {attempt} 次短按 4 未生效，安全重试"
                    f"（下一次 {attempt + 1}/3）")
            ctx.sleep(0.30)
        return None

    def _run_three_fish(self) -> str:
        ctx = self.ctx
        bot = FishingBot(log=ctx.log, debug=False)
        finished = threading.Event()

        def bridge():
            paused = None
            while not finished.wait(0.10):
                if ctx.should_stop():
                    bot.stop()
                    return
                if paused != ctx.paused:
                    paused = ctx.paused
                    bot.set_paused(paused)

        monitor = threading.Thread(target=bridge, name="DailyFishingBridge", daemon=True)
        monitor.start()
        try:
            bot.run(count=3, exit_after=False)
        finally:
            finished.set()
            monitor.join(timeout=1.0)
        dev_log(f"[daily fishing] FishingBot结束 caught={bot.caught}/3 "
                f"stop_requested={ctx.should_stop()}")
        if ctx.should_stop():
            return TaskResult.ABORT
        if bot.caught >= 3:
            self._close_final_settlements(getattr(bot, "rec", None))
            if not self._exit_fishing_mode(getattr(bot, "rec", None)):
                ctx.log(f"{self.name}:完成钓鱼但未确认退出钓鱼界面")
                dev_log(f"[daily fishing] 三条已完成但退出钓鱼模式确认失败 caught={bot.caught}/3")
                return TaskResult.FAIL
            ctx.log(f"{self.name}:已完成 3/3")
            dev_log(f"[daily fishing] 每日钓鱼成功 caught={bot.caught}/3 exit_confirmed=True")
            return TaskResult.SUCCESS
        ctx.log(f"{self.name}:钓鱼提前结束，仅完成 {bot.caught}/3")
        dev_log(f"[daily fishing] 钓鱼提前结束 caught={bot.caught}/3")
        return TaskResult.FAIL

    def _close_final_settlements(self, recognizer) -> None:
        '清理第三条鱼的结算层；个人记录按 F，普通渔获提示等待自然消失。'
        if recognizer is None:
            dev_log("[daily fishing] 第三条结算清理跳过: recognizer=None")
            return
        ctx = self.ctx
        last_state = "UNKNOWN"
        for attempt in range(12):
            if ctx.should_stop():
                return
            frame = ctx.grab()
            if frame is None:
                ctx.sleep(0.15)
                continue
            if recognizer.is_record_screen(frame):
                ctx.log(f"{self.name}:关闭第三条鱼的个人记录界面")
                dev_log(f"[daily fishing] 第三条结算层 attempt={attempt + 1} type=record action=F")
                if not ctx.press("f"):
                    dev_log("[daily fishing] 第三条个人记录层按 F 失败")
                    return
                ctx.sleep(0.55)
                continue
            state, scores = recognizer.classify(frame)
            last_state = state
            reward_visible = recognizer.is_success(frame)
            dev_log(f"[daily fishing] 第三条结算复核 attempt={attempt + 1} "
                    f"state={state} reward={reward_visible} scores={scores}")
            if reward_visible or state == "RESULT_OR_TRANSITION":
                
                ctx.sleep(0.25)
                continue
            if state in self.FISHING_ACTIVE_STATES:
                dev_log(f"[daily fishing] 第三条结算层已清空 state={state}")
                return
            ctx.sleep(0.20)
        dev_log(f"[daily fishing] 第三条结算清理到达等待上限 last_state={last_state}; "
                "交由 ESC 退出链继续处理")

    def _exit_fishing_mode(self, recognizer) -> bool:
        '三条完成后最多按两次 ESC；钓鱼状态消失是主判据，HUD 提供退出正证据。'
        if recognizer is None:
            dev_log("[daily fishing] 退出钓鱼模式失败: recognizer=None")
            return False
        ctx = self.ctx
        for esc_attempt in range(1, 3):
            before = ctx.grab()
            if before is None:
                before_state, before_hits, before_blocked = "NO_FRAME", (), False
            else:
                before_state = self._fishing_state(before, recognizer)
                before_hits, before_blocked = self._world_hud_diagnostics(before)
            dev_log(f"[daily fishing] ESC#{esc_attempt} 前 state={before_state} "
                    f"hud_hits={list(before_hits)} blocked={before_blocked}")
            ctx.log(f"{self.name}:三次钓鱼完成，按 ESC 退出钓鱼界面"
                    + ("（第二次确认）" if esc_attempt == 2 else ""))
            if not ctx.press("esc"):
                dev_log(f"[daily fishing] ESC#{esc_attempt} 注入失败")
                return False

            non_fishing_streak = 0
            hud_seen: set[str] = set()
            observed = {"state": "NO_FRAME", "hits": (), "blocked": False}

            def exited(frame):
                nonlocal non_fishing_streak
                state = self._fishing_state(frame, recognizer)
                hits, blocked = self._world_hud_diagnostics(frame)
                observed.update(state=state, hits=hits, blocked=blocked)
                if state in self.FISHING_ACTIVE_STATES:
                    non_fishing_streak = 0
                    hud_seen.clear()
                    return False
                strong_hud = len(hits) >= 2 and not blocked
                if strong_hud:
                    return True
                if state == "RESULT_OR_TRANSITION" or blocked:
                    non_fishing_streak = 0
                    hud_seen.clear()
                    return False
                non_fishing_streak += 1
                hud_seen.update(hits)
                
                return non_fishing_streak >= 3 and bool(hud_seen)

            ok = bool(ctx.wait_until(exited, timeout=3.0, interval=0.25,
                                     desc=f"退出钓鱼界面#{esc_attempt}"))
            dev_log(f"[daily fishing] ESC#{esc_attempt} 后 state={observed['state']} "
                    f"hud_hits={list(observed['hits'])} blocked={observed['blocked']} "
                    f"hud_seen={sorted(hud_seen)} non_fishing_streak={non_fishing_streak} "
                    f"confirmed={ok}")
            if ok:
                return True
            if esc_attempt == 1:
                ctx.log(f"{self.name}:首次 ESC 后仍未退出钓鱼状态，重试一次")
                ctx.sleep(0.20)
        return False

    def run(self) -> str:
        ctx = self.ctx
        recognizer = FishingRecognizer()
        if not recognizer.ready:
            ctx.log(f"{self.name}:钓鱼识别模板未就绪")
            return TaskResult.FAIL
        if not self._navigate_to_yungen_pier() or not self._walk_to_shore():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        rod_state = self._equip_rod_if_needed(recognizer)
        if not rod_state:
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        if rod_state != "waiting":
            if not ctx.drag_camera(0, steps=8, dy_px=-55):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            ctx.sleep(0.35)
        if rod_state == "equipped":
            active_state = self._activate_equipped_rod(recognizer, verified_equipped=True)
            if not active_state:
                ctx.log(f"{self.name}:装备候选后未进入钓鱼状态，停止以避免误用道具")
                return TaskResult.FAIL
        return self._run_three_fish()
