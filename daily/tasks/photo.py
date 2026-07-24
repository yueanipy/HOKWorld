'拍照任务:ESC 菜单 → 相机 → 快门 → 分享页确认。'
from __future__ import annotations

import time

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult


class PhotoTask(DailyTask):
    task_id = "photo"
    name = "拍照"
    ENTER_CAMERA_TIMEOUT = 5.0
    SHARE_WAIT_S = (2.2, 1.8, 1.8)

    def run(self) -> str:
        ctx = self.ctx
        started = time.monotonic()
        if not nav.open_esc_menu(ctx):
            return TaskResult.FAIL
        
        ctx.click(R.PT_ICON_CAMERA)
        if not ctx.wait_until(
                rec.in_camera, timeout=self.ENTER_CAMERA_TIMEOUT,
                interval=0.20, desc="进入相机"):
            return TaskResult.FAIL

        
        
        for attempt, timeout in enumerate(self.SHARE_WAIT_S, start=1):
            if ctx.should_stop():
                return TaskResult.ABORT
            if not ctx.press("f"):
                return TaskResult.FAIL
            if ctx.wait_until(
                    rec.in_share_page, timeout=timeout, interval=0.20,
                    desc=f"拍照完成确认 {attempt}/{len(self.SHARE_WAIT_S)}"):
                elapsed = time.monotonic() - started
                ctx.log(f"拍照完成({elapsed:.1f}秒)")
                nav.back_to_world(ctx)
                return TaskResult.SUCCESS

            frame = ctx.grab()
            page = rec.photo_page(frame) if frame is not None else ""
            if page == "share":
                nav.back_to_world(ctx)
                return TaskResult.SUCCESS
            if page != "camera":
                ctx.log(f"快门后页面状态未知，停止重复按键(attempt={attempt})")
                break
            ctx.log(f"仍在相机页，重试快门 {attempt}/{len(self.SHARE_WAIT_S)}")

        nav.back_to_world(ctx)
        return TaskResult.FAIL
