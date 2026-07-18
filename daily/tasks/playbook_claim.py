'朝闻道每日活跃度：入口 → 推荐任务领取 → 活跃度奖励领取。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult


def _enter_daily_from_hub(ctx, frame) -> bool:
    '只点击入口页的朝闻道卡片；今日已完成时保持原界面并返回 False。'
    if rec.playbook_daily_done(frame):
        return False
    pt = rec.find_playbook_daily_entry(frame)
    if not pt or not ctx.click(pt):
        return False
    return bool(ctx.wait_until(rec.in_playbook, timeout=10.0, interval=0.35,
                               desc="进入朝闻道详情"))


def open_playbook(ctx) -> bool:
    '进入朝闻道详情；支持世界、ESC 菜单、玩法手册入口页和详情页任意起点。'
    frame = ctx.grab()
    if frame is not None:
        if rec.in_playbook(frame):
            return True
        if rec.in_playbook_hub(frame):
            return _enter_daily_from_hub(ctx, frame)

    if not nav.open_esc_menu(ctx):
        return False
    frame = ctx.grab()
    tile = (rec.find_tile(frame, "玩法") if frame is not None else None) or R.PT_TILE_PLAYBOOK
    if not ctx.click(tile):
        return False

    state = ctx.wait_until(
        lambda f: "detail" if rec.in_playbook(f) else ("hub" if rec.in_playbook_hub(f) else None),
        timeout=10.0, interval=0.35, desc="进入玩法手册")
    if state == "detail":
        return True
    if state == "hub":
        frame = ctx.grab()
        return bool(frame is not None and _enter_daily_from_hub(ctx, frame))
    return False


def _dismiss_reward_once(ctx, assume_visible: bool = False) -> bool:
    '奖励浮层只点一次空白，并确认回到详情页。'
    if not assume_visible:
        frame = ctx.grab()
        if frame is None or not rec.reward_overlay(frame):
            return False
    if not ctx.click(R.PT_REWARD_BLANK):
        return False
    ctx.wait_until(rec.in_playbook, timeout=5.0, interval=0.25, desc="关闭获得奖励浮层")
    return True


class PlaybookClaimTask(DailyTask):
    task_id = "playbook_claim"
    name = "朝闻道·领每日活跃度"

    def run(self) -> str:
        ctx = self.ctx
        if not open_playbook(ctx):
            frame = ctx.grab()
            if frame is not None and rec.playbook_daily_done(frame):
                ctx.log("朝闻道:入口显示今日已完成,保持当前界面并停止")
                return TaskResult.SUCCESS
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        claimed_tasks = 0
        claimed_rewards = 0

        
        
        if not ctx.click(R.PT_SUBTAB_RECOMMEND):
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        ctx.sleep(0.55)

        for _ in range(10):
            if ctx.should_stop():
                return TaskResult.ABORT
            frame = ctx.grab()
            if frame is None:
                break
            pt = rec.find_claimable_daily_card(frame)
            if not pt:
                if rec.reward_overlay(frame):
                    _dismiss_reward_once(ctx, assume_visible=True)
                    continue
                break
            if not ctx.click(pt):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            claimed_tasks += 1
            ctx.log(f"朝闻道:领取推荐任务 #{claimed_tasks}")
            ctx.sleep(0.65)

        
        
        for _ in range(12):
            if ctx.should_stop():
                return TaskResult.ABORT
            frame = ctx.grab()
            if frame is None:
                break
            pt = rec.find_claimable_activity_reward(frame)
            if not pt:
                if rec.reward_overlay(frame):
                    _dismiss_reward_once(ctx, assume_visible=True)
                    continue
                break
            if not ctx.click(pt):
                return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
            claimed_rewards += 1
            ctx.log(f"朝闻道:点击金色活跃度奖励 #{claimed_rewards}")
            
            shown = ctx.wait_until(rec.reward_overlay, timeout=3.0, interval=0.25,
                                   desc="活跃度获得奖励浮层")
            if shown:
                _dismiss_reward_once(ctx, assume_visible=True)
            else:
                ctx.sleep(0.45)

        frame = ctx.grab()
        if frame is not None and rec.reward_overlay(frame):
            _dismiss_reward_once(ctx)
            frame = ctx.grab()
        activity = rec.read_daily_activity(frame) if frame is not None else None
        remaining = rec.find_claimable_activity_reward(frame) if frame is not None else None

        if activity is not None and activity >= 120 and not remaining:
            ctx.log(
                f"朝闻道:每日活跃度 {activity},全部金色奖励已领取"
                f"(任务{claimed_tasks}处/奖励点击{claimed_rewards}次),停留当前界面")
            return TaskResult.SUCCESS
        if not remaining:
            ctx.log(
                f"朝闻道:当前无金色可领取奖励,活跃度={activity if activity is not None else '未识别'};"
                "保持当前界面")
            return TaskResult.SKIP
        ctx.log("朝闻道:仍检测到金色奖励但达到保护循环上限,停止以避免重复点击")
        return TaskResult.FAIL
