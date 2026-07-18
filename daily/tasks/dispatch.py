'宠物派遣:从当前任意派遣状态继续,领取后立即为同一槽重新派遣。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult
from runtime_guard import dev_log


class DispatchTask(DailyTask):
    task_id = "dispatch"
    name = "宠物派遣"

    MAX_PICK = 3
    MAX_SLOTS = 3
    MAX_STEPS = 24       

    def check_available(self) -> bool:
        '管理地图有完成气泡或可派数量时执行;进图失败交给 run 显式报错。'
        if not nav.enter_manage_map(self.ctx):
            return True
        nodes = rec.read_manage_nodes(self.ctx.grab())
        dispatch = nodes["dispatch"]
        return bool(dispatch["has_done"] or (dispatch["count"] or 0) > 0)

    def _goto_dispatch_page(self) -> bool:
        ctx = self.ctx
        frame = ctx.grab()
        if frame is not None and (rec.in_dispatch_page(frame) or rec.member_drawer_open(frame)):
            dev_log("[daily dispatch] 已处于派遣页中间状态，直接续跑")
            return True
        if not nav.enter_manage_map(ctx):
            return False
        if not nav.teleport_via_node(ctx, R.PT_NODE_DISPATCH):
            return False
        ctx.walk("w", 1.2)
        ctx.press("f")
        return bool(ctx.wait_until(
            rec.in_dispatch_page, timeout=10.0, desc="进入宠物派遣页"))

    def _dismiss_reward(self) -> bool:
        '领取后关掉“获得道具”浮层,等派遣页恢复。'
        ctx = self.ctx
        if not ctx.wait_until(rec.reward_overlay, timeout=5.0, desc="等待派遣奖励浮层"):
            ctx.log("领取后未识别到奖励浮层")
            return False
        ctx.click(R.PT_REWARD_BLANK)
        return bool(ctx.wait_until(
            lambda f: rec.in_dispatch_page(f) and not rec.reward_overlay(f),
            timeout=6.0, desc="关闭派遣奖励浮层"))

    def _claim_current(self, pt) -> bool:
        '领取当前已选完成槽;返回派遣页后仍保持当前槽选中。'
        ctx = self.ctx
        if not ctx.click(pt or R.PT_DISPATCH_BTN):
            return False
        after = ctx.wait_until(
            lambda f: ("reward" if rec.reward_overlay(f) else
                       "member" if rec.read_dispatch_button(f)[0] == "member" else None),
            timeout=6.0, interval=0.2, desc="领取派遣奖励")
        if after == "reward" and not self._dismiss_reward():
            return False
        
        
        ready = ctx.wait_until(
            lambda f: rec.read_dispatch_button(f)[0] == "member" or rec.in_dispatch_page(f),
            timeout=5.0, interval=0.2, desc="领奖后返回派遣页")
        dev_log(f"[daily dispatch] 领取结果 after={after} ready={bool(ready)}")
        return bool(ready)

    def _open_member_drawer(self, pt=None, force: bool = False) -> bool:
        '当前槽处于空闲态时按 F 打开选择成员抽屉。'
        ctx = self.ctx
        f = ctx.grab()
        if f is not None and rec.member_drawer_open(f):
            return True
        if f is None:
            return False
        state, _ = rec.read_dispatch_button(f)
        if state == "go":      
            return True
        if state != "member" and not force:
            return False
        
        if not ctx.press("f"):
            return False
        return bool(ctx.wait_until(
            rec.member_drawer_open, timeout=6.0, desc="打开选择成员"))

    def _circle_states(self, frame) -> list[str]:
        return [rec.pet_row_circle(frame, i) for i in range(len(R.PET_ROW_Y))]

    def _select_available_pets(self) -> int:
        '保留已有打钩,再从上到下点击空心圆,最多选满 3 只;返回已选数。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None:
            return 0
        states = self._circle_states(frame)
        if all(state == "none" for state in states):
            ctx.sleep(0.20)
            retry = ctx.grab()
            if retry is not None:
                states = self._circle_states(retry)
        dev_log(f"[daily dispatch] 成员圆圈初始状态={states}")
        selected = sum(state == "checked" for state in states)
        if selected >= self.MAX_PICK:
            return selected

        for row, state in enumerate(states):
            if state != "empty" or selected >= self.MAX_PICK:
                continue
            pt = (R.PT_PET_CIRCLE_X, R.PET_ROW_Y[row])
            if not ctx.click(pt):
                continue
            changed = ctx.wait_until(
                lambda f, r=row: rec.pet_row_circle(f, r) == "checked",
                timeout=2.0, interval=0.15, desc=f"选择第 {row + 1} 只宠物")
            if changed:
                selected += 1
                ctx.log(f"宠物派遣:已选择第 {row + 1} 行宠物({selected}/{self.MAX_PICK})")
            else:
                ctx.log(f"宠物派遣:第 {row + 1} 行圆圈点击后未变为打钩,跳过")
        return selected

    def _dispatch_current(self, member_pt=None, force_open: bool = False) -> str:
        '派遣当前空闲槽;返回 sent/nopets/fail。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None:
            return "fail"
        before = rec.read_dispatch_slots(frame)
        busy_before = sum(state == "busy" for state in before)
        if not self._open_member_drawer(member_pt, force=force_open):
            dev_log(f"[daily dispatch] 打开成员抽屉失败 force={force_open} slots={before} "
                    f"button={rec.read_dispatch_button(frame)[0]}")
            return "fail"

        selected = self._select_available_pets()
        if selected <= 0:
            ctx.log("宠物派遣:没有空心圆或已选宠物,结束派遣")
            ctx.click(R.PT_MEMBER_CLOSE)
            ctx.wait_until(
                lambda f: not rec.member_drawer_open(f),
                timeout=3.0, desc="关闭选择成员")
            return "no_pets"

        ctx.log(f"宠物派遣:已选 {selected} 只宠物,执行派遣")
        if not ctx.press("f"):
            return "fail"

        def _after_go(frame):
            if rec.dispatch_confirm_dialog(frame)[0]:
                return "confirm"
            slots = rec.read_dispatch_slots(frame)
            button = rec.read_dispatch_button(frame)[0]
            if (not rec.member_drawer_open(frame)
                    and (button == "recall" or sum(s == "busy" for s in slots) > busy_before)):
                return "sent"
            return None

        after = ctx.wait_until(_after_go, timeout=6.0, interval=0.2, desc="提交宠物派遣")
        if after == "confirm":
            frame = ctx.grab()
            in_dialog, confirm_pt = rec.dispatch_confirm_dialog(frame)
            if not in_dialog or not ctx.click(confirm_pt or R.PT_DIALOG_CONFIRM):
                return "fail"
            sent = ctx.wait_until(
                lambda f: (not rec.dispatch_confirm_dialog(f)[0]
                           and (rec.read_dispatch_button(f)[0] == "recall"
                                or sum(s == "busy" for s in rec.read_dispatch_slots(f)) > busy_before)),
                timeout=8.0, interval=0.25, desc="确认少成员派遣")
            dev_log(f"[daily dispatch] 少成员确认后派遣结果={bool(sent)}")
            return "sent" if sent else "fail"
        dev_log(f"[daily dispatch] 派遣提交结果={after} before_slots={before}")
        return "sent" if after == "sent" else "fail"

    def _select_slot(self, idx: int, expected: str) -> bool:
        '选择左侧槽并等待右下按钮切到该槽状态。'
        ctx = self.ctx
        if not ctx.click(R.PT_SLOT[idx]):
            return False
        return bool(ctx.wait_until(
            lambda f: rec.read_dispatch_button(f)[0] == expected,
            timeout=4.0, interval=0.2, desc=f"选择派遣槽 {idx + 1}"))

    def _run_page_state_machine(self) -> tuple[int, int, bool, bool]:
        '返回 (领取数,派出数,是否无宠物正常结束,是否发生流程错误)。'
        ctx = self.ctx
        claimed = 0
        sent = 0
        no_pets = False
        failed = False
        refill_current = False
        unknown_scans = 0

        for step in range(self.MAX_STEPS):
            if ctx.should_stop():
                break
            frame = ctx.grab()
            if frame is None:
                failed = True
                dev_log("[daily dispatch] 抓帧失败，停止状态机")
                break
            if rec.reward_overlay(frame):
                if not self._dismiss_reward():
                    failed = True
                    break
                continue

            slots = rec.read_dispatch_slots(frame)
            state, pt = rec.read_dispatch_button(frame)
            drawer = rec.member_drawer_open(frame)
            dev_log(f"[daily dispatch] step={step + 1} slots={slots} button={state} "
                    f"drawer={drawer} refill={refill_current}")

            
            if drawer:
                result = self._dispatch_current(pt)
                if result == "sent":
                    sent += 1
                    refill_current = False
                    continue
                if result == "no_pets":
                    no_pets = True
                else:
                    failed = True
                break

            
            
            if refill_current:
                result = self._dispatch_current(pt, force_open=True)
                if result == "sent":
                    sent += 1
                    refill_current = False
                    continue
                if result == "no_pets":
                    no_pets = True
                else:
                    failed = True
                break

            
            if state == "claim":
                if not self._claim_current(pt):
                    ctx.log("宠物派遣:领取当前槽失败,停止")
                    failed = True
                    break
                claimed += 1
                refill_current = True
                continue

            
            idx = next((i for i, slot in enumerate(slots) if slot == "done"), None)
            if idx is not None:
                if not self._select_slot(idx, "claim"):
                    ctx.log(f"宠物派遣:无法选择完成槽 {idx + 1},停止")
                    failed = True
                    break
                continue

            
            if state in ("member", "go"):
                result = self._dispatch_current(pt)
                if result == "sent":
                    sent += 1
                    continue
                if result == "no_pets":
                    no_pets = True
                else:
                    ctx.log("宠物派遣:当前槽派遣失败,停止")
                    failed = True
                break
            idx = next((i for i, slot in enumerate(slots) if slot == "idle"), None)
            if idx is None:
                if all(slot == "none" for slot in slots) and state == "none" and unknown_scans < 2:
                    unknown_scans += 1
                    ctx.sleep(0.20)
                    continue
                break
            unknown_scans = 0
            if not self._select_slot(idx, "member"):
                ctx.log(f"宠物派遣:无法选择空闲槽 {idx + 1},停止")
                failed = True
                break
        else:
            ctx.log("宠物派遣:状态步骤达到安全上限,停止")
            failed = True

        return claimed, sent, no_pets, failed

    def run(self) -> str:
        ctx = self.ctx
        if not self._goto_dispatch_page():
            return TaskResult.FAIL
        claimed, sent, no_pets, failed = self._run_page_state_machine()
        ctx.log(
            f"宠物派遣:领取 {claimed} 个,派出 {sent} 个"
            + (",无可用宠物" if no_pets else ""))
        ctx.press("esc")
        nav.back_to_world(ctx)
        if ctx.should_stop():
            return TaskResult.ABORT
        return TaskResult.FAIL if failed else TaskResult.SUCCESS
