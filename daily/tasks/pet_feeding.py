'宠物喂食独立任务，按 20260712201920 录制标定。'
from __future__ import annotations

import cv2

import daily.recognizer as rec
from daily import navigation as nav
from daily.base import DailyTask, TaskResult


class PetFeedingTask(DailyTask):
    task_id = "pet_feeding"
    name = "宠物喂食"

    PT_PET_NODE = (0.6193, 0.6620)
    CAMERA_TURN_PX = -619
    WALK_TAP_S = 0.10
    WALK_MAX_STEPS = 12

    ROI_FEED_PROMPT = (0.54, 0.48, 0.68, 0.63)
    ROI_ITEM_PANEL = (0.67, 0.00, 1.00, 1.00)
    ROI_CONFIRM_BUTTON = (0.70, 0.895, 0.96, 0.945)
    PT_FISH = (0.835, 0.112)
    PT_QUALITY_FISH = (0.835, 0.235)
    CONFIRM_BRIGHTNESS_MIN = 175.0

    @staticmethod
    def _feed_prompt(frame) -> bool:
        return "喂食" in rec.ocr_text(frame, PetFeedingTask.ROI_FEED_PROMPT)

    @staticmethod
    def _item_panel_open(frame) -> bool:
        text = rec.ocr_text(frame, PetFeedingTask.ROI_ITEM_PANEL)
        return "选择道具" in text or ("鱼肉" in text and "确定" in text)

    @staticmethod
    def _confirm_active(frame) -> bool:
        f = rec.normalize(frame)
        h, w = f.shape[:2]
        x0, y0, x1, y1 = PetFeedingTask.ROI_CONFIRM_BUTTON
        sub = f[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
        if sub.size == 0:
            return False
        return float(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY).mean()) >= PetFeedingTask.CONFIRM_BRIGHTNESS_MIN

    def _turn_camera(self) -> bool:
        ctx = self.ctx
        from runtime_guard import dev_log

        for attempt in (1, 2, 3):
            if ctx.should_stop():
                return False
            if ctx.drag_camera(self.CAMERA_TURN_PX):
                ctx.sleep(0.9)
                dev_log(f"[daily] {self.name}: 镜头左转 {self.CAMERA_TURN_PX}px(第{attempt}次)")
                return True
            ctx.sleep(0.8)
        ctx.log(f"{self.name}:镜头转动失败")
        return False

    def _walk_to_feeding(self) -> bool:
        ctx = self.ctx
        from runtime_guard import dev_log

        for step in range(self.WALK_MAX_STEPS + 1):
            if ctx.should_stop():
                return False
            frame = ctx.grab()
            if frame is not None and self._feed_prompt(frame):
                ctx.log(f"{self.name}:第 {step} 步识别到“喂食” → 停止移动")
                return True
            if step == self.WALK_MAX_STEPS:
                break
            ctx.tap("w", self.WALK_TAP_S)
            ctx.sleep(0.45)
            dev_log(f"[daily] {self.name}: W 碎步 {step + 1}/{self.WALK_MAX_STEPS}")
        ctx.log(f"{self.name}:走满 {self.WALK_MAX_STEPS} 步仍未识别到“喂食”")
        return False

    def _select_food(self) -> bool:
        ctx = self.ctx
        from runtime_guard import dev_log

        for name, point in (("鱼肉", self.PT_FISH), ("优质鱼肉", self.PT_QUALITY_FISH)):
            if ctx.should_stop():
                return False
            ctx.click(point)
            ctx.sleep(0.7)
            frame = ctx.grab()
            active = frame is not None and self._confirm_active(frame)
            dev_log(f"[daily] {self.name}: 选择{name} → 确认按钮{'亮' if active else '灰'}")
            if active:
                ctx.log(f"{self.name}:已选择{name}，确认按钮变亮")
                return True
        ctx.log(f"{self.name}:鱼肉和优质鱼肉均未使确认按钮变亮 → ESC 退出")
        ctx.press("esc")
        ctx.sleep(0.5)
        return False

    def run(self) -> str:
        ctx = self.ctx
        if not nav.enter_manage_map(ctx):
            return TaskResult.FAIL
        if not nav.teleport_via_node(ctx, self.PT_PET_NODE):
            return TaskResult.FAIL
        if not ctx.wait_until(rec.homeland_loaded, timeout=15.0, interval=0.5,
                              desc="宠物节点传送完成(左上居所标题)"):
            return TaskResult.FAIL
        ctx.sleep(0.8)
        if not self._turn_camera():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        if not self._walk_to_feeding():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        ctx.press("f")
        if not ctx.wait_until(self._item_panel_open, timeout=5.0, interval=0.3,
                              desc="宠物喂食道具界面"):
            return TaskResult.FAIL
        if not self._select_food():
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        ctx.press("f")
        closed = ctx.wait_until(lambda f: not self._item_panel_open(f), timeout=5.0,
                                interval=0.3, desc="确认宠物喂食")
        if not closed:
            ctx.press("esc")
            return TaskResult.FAIL
        ctx.log(f"{self.name}:喂食确认完成")
        return TaskResult.SUCCESS
