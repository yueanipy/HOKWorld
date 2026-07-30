'兴趣圈点赞任务:朝闻道·会友 → 浏览兴趣圈任务卡 → 进兴趣圈 → 双击第一个赞 → 返回日常页。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult
from daily.tasks.playbook_claim import open_playbook
from runtime_guard import dev_log


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

    def _complete_like(self, pt, *, initially_gold: bool) -> bool:
        '在兴趣圈页面只对第一个赞固定点击两次。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None or not rec.in_interest_circle(frame):
            dev_log("[interest] double like blocked outside interest page")
            return False
        dev_log(
            f"[interest] like initial={'gold' if initially_gold else 'gray'} "
            f"point=({pt[0]:.4f},{pt[1]:.4f})"
        )
        for click_index in range(1, 3):
            if not ctx.click(pt):
                dev_log(
                    f"[interest] double like click rejected "
                    f"index={click_index}/2")
                return False
            dev_log(
                f"[interest] double like click index={click_index}/2")
            ctx.sleep(0.30 if click_index == 1 else 0.25)
        ctx.log("兴趣圈:已在点赞页面对第一个赞点击两次")
        return True

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
        
        ctx.sleep(0.60)
        entry = None
        for attempt in range(5):
            if ctx.should_stop():
                return TaskResult.ABORT
            frame = ctx.grab()
            if frame is None:
                ctx.sleep(0.35)
                continue
            scan = rec.scan_interest_task_entry(frame)
            boxes = scan["boxes"]
            dev_log(f"[interest ocr] attempt={attempt + 1}/5 boxes={len(boxes)}")
            for text, x0, y0, x1, y1 in boxes:
                dev_log(
                    f"[interest ocr] text={text!r} "
                    f"box=({x0:.4f},{y0:.4f},{x1:.4f},{y1:.4f})")
            entry = scan["entry"]
            if entry is not None:
                break
            ctx.log(f"兴趣圈:第{attempt + 1}次未识别到“浏览一次兴趣圈”，不点击任何卡片")
            if attempt < 4:
                ctx.sleep(0.35)
        if not entry:
            dev_log("[interest] no exact browse-interest entry; card click blocked")
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL
        card_pt = entry["pt"]
        normalized_entry = entry["normalized"]
        if "浏览1次兴趣圈" not in normalized_entry or "拍照" in normalized_entry:
            dev_log(
                f"[interest] unsafe entry rejected text={entry['text']!r} "
                f"point=({card_pt[0]:.4f},{card_pt[1]:.4f})")
            nav.back_to_world(ctx)
            return TaskResult.FAIL
        ctx.log(
            f"兴趣圈:只命中“浏览1次兴趣圈” mode={entry['mode']} "
            f"point=({card_pt[0]:.3f},{card_pt[1]:.3f}) text={entry['text']!r}")
        dev_log(
            f"[interest] entry mode={entry['mode']} "
            f"point=({card_pt[0]:.4f},{card_pt[1]:.4f}) text={entry['text']!r}")
        if not ctx.click(card_pt):
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        entered = bool(ctx.wait_until(
            rec.in_interest_circle, timeout=15.0, interval=0.30, desc="进入兴趣圈"))
        if not entered:
            frame = ctx.grab()
            entered = bool(frame is not None and rec.in_interest_circle(frame))
            if entered:
                dev_log("[interest] entered on final frame after wait timeout")
        if not entered:
            frame = ctx.grab()
            if frame is not None:
                ctx.log(
                    "兴趣圈:点击任务文字后未进入 "
                    f"playbook={rec.in_playbook(frame)} "
                    f"photo_page={rec.photo_page(frame)!r} "
                    f"in_circle={rec.in_interest_circle(frame)}")
                dev_log(
                    "[interest] entry click failed to enter "
                    f"playbook={rec.in_playbook(frame)} "
                    f"photo_page={rec.photo_page(frame)!r} "
                    f"in_circle={rec.in_interest_circle(frame)}")
            nav.back_to_world(ctx)
            return TaskResult.FAIL
        dev_log("[interest] entered interest circle")
        if not self._dismiss_badge_popup():
            dev_log("[interest] failed to dismiss entry badge popup")
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        
        buttons = ctx.wait_until(rec.find_like_buttons, timeout=8.0, interval=0.25, desc="兴趣圈点赞按钮")
        if not buttons:
            frame = ctx.grab()
            if frame is not None:
                diag = rec.like_button_match_diagnostics(frame)
                ctx.log(
                    "兴趣圈:未定位点赞按钮 "
                    f"in_circle={rec.in_interest_circle(frame)} "
                    f"popup={rec.interest_badge_popup(frame)} "
                    f"best={diag.get('best_score', 0.0):.3f} "
                    f"scale={diag.get('best_scale', 1.0):.2f} "
                    f"reason={diag.get('reason', 'unknown')}"
                )
            nav.back_to_world(ctx)
            return TaskResult.FAIL
        first = buttons[0]
        pt = first["pt"]
        ctx.log(f"兴趣圈:识别到{len(buttons)}个点赞按钮,只处理第一个({pt[0]:.3f},{pt[1]:.3f})")
        dev_log(
            f"[interest] buttons={len(buttons)} first_score={first.get('score', 0.0):.3f} "
            f"scale={first.get('scale', 1.0):.2f} "
            f"state={'gold' if first['gold'] else 'gray'}"
        )
        if not self._complete_like(pt, initially_gold=bool(first["gold"])):
            dev_log("[interest] double like was not completed")
            nav.back_to_world(ctx)
            return TaskResult.ABORT if ctx.should_stop() else TaskResult.FAIL

        
        ctx.press("esc")
        returned = bool(ctx.wait_until(
            rec.in_playbook, timeout=4.0, interval=0.35, desc="返回日常任务界面"))
        dev_log(f"[interest] task success returned_to_playbook={returned}")
        return TaskResult.SUCCESS
