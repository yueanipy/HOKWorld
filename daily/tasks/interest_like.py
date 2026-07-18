'兴趣圈点赞任务:朝闻道·会友 → 浏览兴趣圈任务卡 → 进兴趣圈 → 点第一个 👍 → 返回日常页。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult
from daily.tasks.playbook_claim import open_playbook


class InterestLikeTask(DailyTask):
    task_id = "interest_like"
    name = "兴趣圈点赞"
    POPUP_DISMISS_PT = (0.10, 0.82)

    def _dismiss_badge_popup(self) -> bool:
        '关闭可能挡住点赞按钮的徽章升级层；无弹层时不执行点击。'
        ctx = self.ctx
        for _ in range(2):
            frame = ctx.grab()
            if frame is None:
                return False
            if not rec.interest_badge_popup(frame):
                return True
            ctx.log("兴趣圈:检测到徽章升级奖励弹层，点击中央区域外空白关闭")
            if not ctx.click(self.POPUP_DISMISS_PT):
                return False
            if ctx.wait_until(
                    lambda current: not rec.interest_badge_popup(current),
                    timeout=3.0, interval=0.20, desc="关闭兴趣圈徽章奖励弹层"):
                return True
        return False

    def run(self) -> str:
        ctx = self.ctx
        
        if not open_playbook(ctx):
            frame = ctx.grab()
            if frame is not None and rec.playbook_daily_done(frame):
                ctx.log("兴趣圈:朝闻道今日已完成,无需再点赞;保持当前界面")
                return TaskResult.SUCCESS
            return TaskResult.FAIL
        
        if not ctx.click(R.PT_SUBTAB_SOCIAL):
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        card_pt = ctx.wait_until(rec.find_interest_task_card, timeout=5.0, interval=0.35,
                                 desc="会友·兴趣圈任务卡")
        if not card_pt or not ctx.click(card_pt):
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        if not ctx.wait_until(rec.in_interest_circle, timeout=10.0, desc="进入兴趣圈"):
            nav.back_to_world(ctx)
            return TaskResult.FAIL
        if not self._dismiss_badge_popup():
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        
        buttons = ctx.wait_until(rec.find_like_buttons, timeout=8.0, interval=0.25, desc="兴趣圈点赞按钮")
        if not buttons:
            nav.back_to_world(ctx)
            return TaskResult.FAIL
        first = buttons[0]
        pt = first["pt"]
        ctx.log(f"兴趣圈:识别到{len(buttons)}个点赞按钮,只处理第一个({pt[0]:.3f},{pt[1]:.3f})")

        if first["gold"]:
            if not ctx.click(pt):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            ctx.sleep(0.20)
            if not self._dismiss_badge_popup():
                nav.back_to_world(ctx)
                return TaskResult.FAIL
            became_gray = ctx.wait_until(lambda frame: not rec.like_is_gold(frame, pt),
                                         timeout=3.0, interval=0.25, desc="取消已有点赞")
            if not became_gray:
                nav.back_to_world(ctx)
                return TaskResult.FAIL
            if not ctx.click(pt):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            ctx.log("兴趣圈:第一个作品原为金色,已取消后重新点赞")
        else:
            if not ctx.click(pt):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            ctx.log("兴趣圈:已点击第一个灰色点赞按钮")

        
        ctx.sleep(0.20)
        if not self._dismiss_badge_popup():
            nav.back_to_world(ctx)
            return TaskResult.FAIL

        if not ctx.wait_until(lambda frame: rec.like_is_gold(frame, pt),
                              timeout=3.0, interval=0.25, desc="点赞变为金色"):
            nav.back_to_world(ctx)
            return TaskResult.FAIL

        
        ctx.press("esc")
        ctx.wait_until(rec.in_playbook, timeout=4.0, interval=0.35, desc="返回日常任务界面")
        return TaskResult.SUCCESS
