'拍照任务:ESC 菜单 → 相机 → 等 F 快门 → 拍 → 回到世界。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult


class PhotoTask(DailyTask):
    task_id = "photo"
    name = "拍照"

    def run(self) -> str:
        ctx = self.ctx
        if not nav.open_esc_menu(ctx):
            return TaskResult.FAIL
        
        ctx.click(R.PT_ICON_CAMERA)
        if not ctx.wait_until(rec.in_camera, timeout=8.0, desc="进入相机"):
            return TaskResult.FAIL
        
        ctx.wait_until(rec.camera_f_ready, timeout=6.0, desc="等待快门就绪")
        ctx.press("f")
        
        if ctx.wait_until(rec.in_share_page, timeout=6.0, desc="拍照完成(分享页)"):
            ctx.log("拍照完成")
            nav.back_to_world(ctx)
            return TaskResult.SUCCESS
        
        ctx.press("f")
        if ctx.wait_until(rec.in_share_page, timeout=5.0):
            nav.back_to_world(ctx)
            return TaskResult.SUCCESS
        nav.back_to_world(ctx)
        return TaskResult.FAIL
