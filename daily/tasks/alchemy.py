'每日任务：寻路到荣耀塔制药台，并确认制作累计三份药。'
from __future__ import annotations

from difflib import SequenceMatcher

import daily.recognizer as rec
from daily import regions as R
from daily.base import TaskResult
from daily.tasks._glory_tower_route import GloryTowerRouteTask, TowerRouteSpec
from runtime_guard import dev_log


class AlchemyTask(GloryTowerRouteTask):
    task_id = "alchemy"
    name = "制药（制作三份）"
    CRAFT_LABEL = "制药"
    RECIPE_LABEL = "药方"
    DEV_TAG = "alchemy"
    TARGET_COUNT = 3
    MAX_RECIPE_SCROLLS = 10
    MAX_CRAFT_ROUNDS = 8

    route = TowerRouteSpec(
        target_word="制药",
        
        
        turn_total_px=1000,
        coarse_pulses=8,
        recovery_side="a",
        fine_pulses=5,
        continuous_stop_word="制药",
        
        continuous_walk_timeout_s=4.0,
        turn_landmark_word="荣耀塔",
        post_landmark_turn_px=0,
        turn_landmark_timeout_s=2.0,
        
        
        
        
        
        
        telescope_turn_step_px=150,
        telescope_turn_max_px=900,
        telescope_turn_stages_px=(400, 200, 150, 150),
        telescope_move_after_first_stage=True,
        telescope_turn_timeout_s=1.6,
        telescope_failure_timeout_s=3.0,
        telescope_post_turn_walk_s=0.0,
        telescope_recovery_side="",
        telescope_recovery_step_s=0.08,
        telescope_recovery_scan_px=(-120, 240, -120),
        climb_overshoot_recovery=True,
        climb_recovery_back_steps=2,
        climb_recovery_side="d",
        climb_recovery_side_steps=2,
        climb_recovery_step_s=0.09,
        interact_on_arrival=True,
        narrow_alchemy_prompt=True,
    )

    def _in_craft_page(self, frame) -> bool:
        return rec.in_crafting_page(frame, self.CRAFT_LABEL)

    def _dev_log(self, message: str) -> None:
        dev_log(f"[{self.DEV_TAG}] {message}")

    @staticmethod
    def _same_recipe(left: str, right: str) -> bool:
        if not left or not right:
            return False
        return (left in right or right in left
                or SequenceMatcher(None, left, right).ratio() >= 0.72)

    def _select_recipe(self, row: dict) -> bool:
        '点击 OCR 配方行，并要求右侧选中标题确实切换到该物品。'
        ctx = self.ctx
        name = str(row["name"])
        if not ctx.click(row["point"]):
            return False

        def selected(frame):
            return self._same_recipe(name, rec.alchemy_selected_name(frame))

        if not ctx.wait_until(selected, timeout=2.5, interval=0.18,
                              desc=f"选择药方 {name}"):
            ctx.log(f"{self.CRAFT_LABEL}:点击“{name}”后右侧标题没有切换，按不可用处理")
            return False
        frame = ctx.grab()
        if frame is None or not rec.alchemy_can_craft(frame):
            ctx.log(f"{self.CRAFT_LABEL}:“{name}”右侧素材不足或制作控件不可用")
            return False
        return True

    def _find_craftable_recipe(self, blocked: set[str]) -> str | None:
        '优先当前药方；否则扫描可见行，再有界上下翻页查找。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None or not self._in_craft_page(frame):
            return None

        selected = rec.alchemy_selected_name(frame)
        if selected and selected not in blocked and rec.alchemy_can_craft(frame):
            ctx.log(f"{self.CRAFT_LABEL}:继续使用当前可制作{self.RECIPE_LABEL}“{selected}”")
            return selected

        
        directions = (
            (R.PT_ALCHEMY_LIST_DRAG_BOTTOM, R.PT_ALCHEMY_LIST_DRAG_TOP, "向后"),
            (R.PT_ALCHEMY_LIST_DRAG_TOP, R.PT_ALCHEMY_LIST_DRAG_BOTTOM, "向前"),
        )
        attempted: set[str] = set()
        for start, end, label in directions:
            previous_signature: tuple[str, ...] | None = None
            stagnant = 0
            for scroll_index in range(self.MAX_RECIPE_SCROLLS + 1):
                if ctx.should_stop():
                    return None
                frame = ctx.grab()
                if frame is None or not self._in_craft_page(frame):
                    return None
                rows = rec.alchemy_recipe_rows(frame)
                signature = tuple(str(row["name"]) for row in rows)
                self._dev_log(
                    f"扫描{self.RECIPE_LABEL} direction={label} step={scroll_index} "
                    f"rows={[(r['name'], r['locked'], r['insufficient']) for r in rows]}")

                for row in rows:
                    name = str(row["name"])
                    if not row["available"] or name in blocked or name in attempted:
                        continue
                    attempted.add(name)
                    ctx.log(f"{self.CRAFT_LABEL}:尝试可制作{self.RECIPE_LABEL}“{name}”")
                    if self._select_recipe(row):
                        return name
                    blocked.add(name)

                if scroll_index >= self.MAX_RECIPE_SCROLLS:
                    break
                stagnant = stagnant + 1 if signature == previous_signature else 0
                previous_signature = signature
                if stagnant >= 2:
                    self._dev_log(f"{self.RECIPE_LABEL}列表{label}连续两次无新内容，切换方向")
                    break
                if not ctx.drag(start, end, duration_s=0.45):
                    return None
                ctx.sleep(0.30)
        return None

    def _read_quantity(self, retries: int = 3) -> int | None:
        ctx = self.ctx
        for attempt in range(retries):
            frame = ctx.grab()
            quantity = rec.alchemy_quantity(frame) if frame is not None else None
            if quantity is not None:
                return quantity
            if attempt + 1 < retries:
                ctx.sleep(0.12)
        return None

    def _change_quantity_once(self, current: int, increase: bool) -> int | None:
        '点击一次加减号，并以数字图像变化确认游戏实际接受了点击。'
        ctx = self.ctx
        point = R.PT_ALCHEMY_PLUS if increase else R.PT_ALCHEMY_MINUS
        expected = current + 1 if increase else current - 1
        for attempt in range(2):
            if not ctx.click(point):
                return None

            def changed(frame):
                quantity = rec.alchemy_quantity(frame)
                return quantity if quantity != current else None

            changed_to = ctx.wait_until(changed, timeout=1.0, interval=0.12,
                                        desc=f"调整{self.CRAFT_LABEL}数量")
            if changed_to is not None:
                value = int(changed_to)
                self._dev_log(f"数量调整 {'+' if increase else '-'} "
                              f"attempt={attempt + 1} {current}->{value} expected={expected}")
                return value
        return current

    def _set_and_confirm_quantity(self, target: int) -> int | None:
        '闭环调到目标数量；最终返回的是画面识别值，不按点击次数推算。'
        ctx = self.ctx
        target = max(1, min(self.TARGET_COUNT, int(target)))
        current = self._read_quantity()
        if current is None:
            ctx.log(f"{self.CRAFT_LABEL}:无法读取当前制作数量，本轮不点击制作")
            return None

        for _ in range(6):
            if current == target:
                break
            next_value = self._change_quantity_once(current, increase=current < target)
            if next_value is None or next_value == current:
                break
            current = next_value

        
        confirmed: list[int] = []
        for index in range(2):
            quantity = self._read_quantity(retries=2)
            if quantity is None:
                return None
            confirmed.append(quantity)
            if index == 0:
                ctx.sleep(0.12)
        if confirmed[0] != confirmed[1]:
            ctx.log(f"{self.CRAFT_LABEL}:制作数量识别不稳定 {confirmed}，本轮不制作")
            return None
        actual = confirmed[-1]
        if not 1 <= actual <= target:
            ctx.log(f"{self.CRAFT_LABEL}:实际数量 {actual} 超出本轮目标 {target}，本轮不制作")
            return None
        ctx.log(f"{self.CRAFT_LABEL}:已确认本轮实际制作数量 {actual}")
        return actual

    def _craft_current(self, recipe: str, remaining: int) -> int:
        '制作当前药方；只有完成浮层成立才返回已制作数量，否则返回 0。'
        ctx = self.ctx
        quantity = self._set_and_confirm_quantity(remaining)
        if quantity is None:
            return 0

        frame = ctx.grab()
        if frame is None:
            return 0
        selected = rec.alchemy_selected_name(frame)
        if not self._same_recipe(recipe, selected):
            ctx.log(f"{self.CRAFT_LABEL}:制作前{self.RECIPE_LABEL}发生变化 "
                    f"{recipe!r}->{selected!r}，取消点击")
            return 0
        if rec.alchemy_quantity(frame) != quantity or not rec.alchemy_can_craft(frame):
            ctx.log(f"{self.CRAFT_LABEL}:制作前数量或素材二次确认失败，取消点击")
            return 0
        button = rec.alchemy_craft_button(frame) or R.PT_ALCHEMY_CRAFT
        if not ctx.click(button):
            return 0

        
        ctx.sleep(3.0)
        completed = ctx.wait_until(rec.alchemy_complete_overlay, timeout=6.0, interval=0.25,
                                   desc="确认制作完成")
        if not completed:
            ctx.log(f"{self.CRAFT_LABEL}:“{recipe}”未识别到制作完成，本轮不计数")
            return 0

        
        ctx.log(f"{self.CRAFT_LABEL}:“{recipe}”制作完成，确认增加 {quantity}，等待关闭结果浮层")
        page_returned = False
        for _ in range(2):
            if not ctx.click(R.PT_ALCHEMY_COMPLETE_BLANK):
                break
            if ctx.wait_until(self._in_craft_page, timeout=3.0, interval=0.22,
                              desc=f"返回{self.CRAFT_LABEL}页面"):
                page_returned = True
                break
        if not page_returned:
            
            ctx.log(f"{self.CRAFT_LABEL}:制作数量已计入，但关闭完成浮层后没有确认回到页面")
        return quantity

    def _leave_to_hud(self) -> bool:
        '完成三份后从制作页退回普通角色 HUD。'
        ctx = self.ctx
        for attempt in range(2):
            if not ctx.press("esc"):
                return False
            if ctx.wait_until(self._world_hud_ready, timeout=5.0, interval=0.30,
                              desc=f"退出{self.CRAFT_LABEL}界面"):
                return True
            frame = ctx.grab()
            if frame is None or not self._in_craft_page(frame):
                break
            ctx.log(f"{self.CRAFT_LABEL}:第 {attempt + 1} 次 Esc 后仍在制作页，允许重试一次")
        return False

    def _craft_three(self) -> str:
        ctx = self.ctx
        made = 0
        blocked: set[str] = set()
        current_recipe = ""

        for round_index in range(self.MAX_CRAFT_ROUNDS):
            if ctx.should_stop():
                return TaskResult.ABORT
            if made >= self.TARGET_COUNT:
                break
            frame = ctx.grab()
            if frame is None or not self._in_craft_page(frame):
                ctx.log(f"{self.CRAFT_LABEL}:制作过程中离开制作页面")
                return TaskResult.FAIL

            selected = rec.alchemy_selected_name(frame)
            if (current_recipe and self._same_recipe(current_recipe, selected)
                    and rec.alchemy_can_craft(frame)):
                recipe = current_recipe
                ctx.log(f"{self.CRAFT_LABEL}:上次{self.RECIPE_LABEL}“{recipe}”仍可制作，继续使用")
            else:
                recipe = self._find_craftable_recipe(blocked)
                if not recipe:
                    ctx.log(f"{self.CRAFT_LABEL}:已确认制作 {made}/{self.TARGET_COUNT}，"
                            f"列表没有其它可制作{self.RECIPE_LABEL}")
                    return TaskResult.FAIL
                current_recipe = recipe

            remaining = self.TARGET_COUNT - made
            crafted = self._craft_current(recipe, remaining)
            self._dev_log(f"制作轮次={round_index + 1} recipe={recipe!r} "
                          f"remaining={remaining} confirmed={crafted} total_before={made}")
            if crafted <= 0:
                blocked.add(recipe)
                current_recipe = ""
                continue
            made += crafted
            ctx.log(f"{self.CRAFT_LABEL}:累计确认 {made}/{self.TARGET_COUNT}")

        if made < self.TARGET_COUNT:
            ctx.log(f"{self.CRAFT_LABEL}:达到安全轮次上限，仅确认 {made}/{self.TARGET_COUNT}")
            return TaskResult.FAIL
        if not self._leave_to_hud():
            ctx.log(f"{self.CRAFT_LABEL}:已完成三份，但没有确认退出到角色 HUD")
            return TaskResult.FAIL
        ctx.log(f"{self.CRAFT_LABEL}:已确认制作三份并返回角色 HUD")
        return TaskResult.SUCCESS

    def _confirm_open_after_arrival(self) -> bool:
        '确认首次 F 生效；已锁存唯一制作台交互时最多补按两次。'
        ctx = self.ctx
        total_presses = 1  
        while total_presses <= 3 and not ctx.should_stop():
            deadline = ctx.logical_time() + 2.4
            last_frame = None
            climb_recovered = False
            while ctx.logical_time() < deadline and not ctx.should_stop():
                frame = ctx.grab()
                last_frame = frame
                
                if frame is not None and rec.climb_key_visible(frame):
                    self._dev_log("F 后等待页面期间识别到 C，立即执行爬墙纠偏")
                    if not self._recover_climb_overshoot(
                            self.CRAFT_LABEL, release_climb=True):
                        return False
                    climb_recovered = True
                    break
                if frame is not None and self._in_craft_page(frame):
                    self._dev_log(
                        f"第 {total_presses} 次 F 后确认进入{self.CRAFT_LABEL}页面")
                    return True
                ctx.sleep(0.18)

            if ctx.should_stop() or total_presses >= 3:
                break
            prompt = self._interaction_text(last_frame) if last_frame is not None else ""
            if climb_recovered:
                self._dev_log(f"爬墙纠偏已重新命中{self.CRAFT_LABEL}，准备补按 F")
            else:
                
                
                self._dev_log(f"第 {total_presses} 次 F 后页面未打开 prompt={prompt!r}")
            ctx.sleep(0.12)
            if not ctx.press("f", hold_s=0.08):
                return False
            total_presses += 1
            self._dev_log(f"页面仍未打开，已补按第 {total_presses} 次 F")
        return False

    def _recover_and_reenter_after_failed_f(self) -> bool:
        '首次 F 闭环失败后只允许一次重新站位；恢复命中后必须发送 F。'
        ctx = self.ctx
        if ctx.should_stop() or not ctx.action_ready():
            return False
        frame = ctx.grab()
        climb_visible = bool(frame is not None and rec.climb_key_visible(frame))
        self._dev_log(
            f"三次 F 未打开页面，开始一次有界重新站位 c_visible={climb_visible}")
        if not self._recover_climb_overshoot(
                self.CRAFT_LABEL, release_climb=climb_visible):
            self._dev_log(f"F 失败后的重新站位未命中{self.CRAFT_LABEL}，不盲按 F")
            return False
        ctx.sleep(0.12)
        dev_log(f"[glory route] {self.task_id} F失败恢复命中，准备强制发送 F")
        if not ctx.press("f", hold_s=0.10):
            self._dev_log("F 失败恢复已命中制药，但重新发送 F 失败")
            return False
        self._dev_log("F 失败恢复命中后已重新发送 F，开始再次确认页面")
        return self._confirm_open_after_arrival()

    def run(self) -> str:
        route_result = super().run()
        if route_result != TaskResult.SUCCESS:
            return route_result
        ctx = self.ctx
        self._dev_log(f"已按 F，开始确认{self.CRAFT_LABEL}页面并允许漏键重试")
        if not self._confirm_open_after_arrival():
            if (self.route.climb_overshoot_recovery
                    and self._recover_and_reenter_after_failed_f()):
                frame = ctx.grab()
                if frame is not None:
                    self._dev_log(
                        f"{self.CRAFT_LABEL}页面已在恢复后打开 "
                        f"selected={rec.alchemy_selected_name(frame)!r}")
                return self._craft_three()
            frame = ctx.grab()
            header = rec.ocr_text(frame, R.ROI_ALCHEMY_HEADER) if frame is not None else "<无画面>"
            controls = rec.ocr_text(frame, R.ROI_ALCHEMY_CONTROLS) if frame is not None else "<无画面>"
            self._dev_log(f"F 后{self.CRAFT_LABEL}页面确认失败 "
                          f"header={header!r} controls={controls!r}")
            self.ctx.log(f"{self.CRAFT_LABEL}:按 F 后没有确认进入制作页面")
            return TaskResult.ABORT if self.ctx.should_stop() else TaskResult.FAIL
        frame = ctx.grab()
        if frame is not None:
            self._dev_log(
                f"{self.CRAFT_LABEL}页面已打开 selected={rec.alchemy_selected_name(frame)!r} "
                f"rows={[(row['name'], row['locked'], row['insufficient']) for row in rec.alchemy_recipe_rows(frame)]}")
        return self._craft_three()
