'宠物派遣：领取完成奖励，并把未派出的配置地区分配给任意可用槽。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult
from daily.config import DailyConfig
from runtime_guard import dev_log


class DispatchTask(DailyTask):
    task_id = "dispatch"
    name = "宠物派遣"

    MAX_PICK = 3
    MAX_SLOTS = 3
    TELEPORT_TIMEOUT_S = 30.0
    NO_PET_CONFIRM_FRAMES = 3
    SLOT_UNKNOWN_RETRIES = 3

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.regions = DailyConfig().dispatch_regions()

    def check_available(self) -> bool:
        '派遣可从任意槽状态续跑，是否需要操作只由派遣页状态机判断。'
        return True

    def _goto_dispatch_page(self) -> bool:
        ctx = self.ctx
        frame = ctx.grab()
        if frame is not None and (rec.in_dispatch_page(frame) or rec.member_drawer_open(frame)):
            dev_log("[daily dispatch] 已处于派遣页中间状态，直接续跑")
            return True
        if not nav.enter_manage_map(ctx):
            return False
        if not nav.teleport_via_node(
                ctx, R.PT_NODE_DISPATCH, self.TELEPORT_TIMEOUT_S):
            dev_log(
                "[daily dispatch] 传送后30秒内未确认角色 HUD，禁止提前发送 W/F")
            return False
        if not ctx.walk("w", 1.2):
            ctx.log("宠物派遣:传送后 W 走位未成功")
            dev_log("[daily dispatch] 角色 HUD 已恢复，但 W 走位输入失败")
            return False
        dev_log("[daily dispatch] 传送完成后 W 1.2s 已发送并完成")
        ctx.sleep(0.20)

        for attempt in (1, 2):
            if not ctx.press("f"):
                ctx.log(f"宠物派遣:第 {attempt} 次 F 交互输入失败")
                return False
            dev_log(f"[daily dispatch] F 交互已发送 attempt={attempt}/2")
            entered = ctx.wait_until(
                rec.in_dispatch_page, timeout=5.0, interval=0.20,
                desc=f"进入宠物派遣页({attempt}/2)")
            if entered:
                return True
            frame = ctx.grab()
            if attempt == 1 and frame is not None and rec.in_world_hud(frame):
                dev_log("[daily dispatch] 首次 F 后仍为角色 HUD，允许原地补一次 F")
                continue
            break
        dev_log("[daily dispatch] W 后两次视觉闭环仍未进入派遣页")
        return False

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

    def _claim_current(self, pt) -> str:
        '领取当前完成槽，返回 member/locked/fail。'
        ctx = self.ctx
        if not ctx.click(pt or R.PT_DISPATCH_BTN):
            return "fail"
        after = ctx.wait_until(
            lambda f: ("reward" if rec.reward_overlay(f) else
                       "member" if rec.read_dispatch_button(f)[0] == "member" else
                       "locked" if rec.dispatch_daily_locked(f) else None),
            timeout=6.0, interval=0.2, desc="领取派遣奖励")
        if not after:
            return "fail"
        if after == "locked":
            dev_log("[daily dispatch] 领取动作后直接进入明日再来")
            return "locked"
        if after == "reward" and not self._dismiss_reward():
            return "fail"
        
        
        ready = ctx.wait_until(
            lambda f: ("member" if rec.read_dispatch_button(f)[0] == "member" else
                       "locked" if rec.dispatch_daily_locked(f) else None),
            timeout=5.0, interval=0.2, desc="领奖后返回派遣页")
        dev_log(f"[daily dispatch] 领取结果 after={after} state={ready}")
        return ready if ready in ("member", "locked") else "fail"

    def _open_member_drawer(self) -> bool:
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
        if state != "member":
            return False
        
        if not ctx.press("f"):
            return False
        return bool(ctx.wait_until(
            rec.member_drawer_open, timeout=6.0, desc="打开选择成员"))

    def _circle_states(self, frame) -> list[str]:
        return rec.pet_row_circles(frame)

    def _select_available_pets(self) -> int:
        '选择最多3只宠物；返回已选数，-1表示存在可选宠物但操作/抓帧失败。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None:
            return -1
        states = self._circle_states(frame)
        none_streak = 1 if all(state == "none" for state in states) else 0
        while 0 < none_streak < self.NO_PET_CONFIRM_FRAMES:
            ctx.sleep(0.20)
            retry = ctx.grab()
            if retry is None:
                return -1
            states = self._circle_states(retry)
            if all(state == "none" for state in states):
                none_streak += 1
            else:
                none_streak = 0
                break
        dev_log(f"[daily dispatch] 成员圆圈初始状态={states}")
        selected = sum(state == "checked" for state in states)
        had_empty = any(state == "empty" for state in states)
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
        if selected <= 0 and had_empty:
            dev_log("[daily dispatch] 检测到空心圆但没有任何一只完成打钩，按流程失败处理")
            return -1
        return selected

    def _select_region(self, target: str) -> str:
        '把当前可用槽切到目标地区，返回 ready/locked/fail。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is None:
            return "fail"
        current = rec.read_dispatch_current_region(frame)
        locked = rec.dispatch_daily_locked(frame)
        button = rec.read_dispatch_button(frame)[0]
        if current == target:
            dev_log(f"[daily dispatch] 当前地区已是 {target}，不重复点击")
            if button in ("member", "go"):
                return "ready"
            if locked:
                return "locked"
            ready = ctx.wait_until(
                lambda f: (rec.read_dispatch_current_region(f) == target
                           and rec.read_dispatch_button(f)[0] in ("member", "go")),
                timeout=3.0, interval=0.20, desc=f"等待地区 {target} 可派遣")
            return "ready" if ready else "fail"

        point = rec.dispatch_region_point(frame, target)
        if point is None:
            ctx.log(f"宠物派遣:地图未找到地区 {target}")
            return "fail"
        if not ctx.click(point):
            return "fail"

        selected = ctx.wait_until(
            lambda f: ("ready" if (rec.read_dispatch_current_region(f) == target
                                    and rec.read_dispatch_button(f)[0] in ("member", "go")) else
                       "locked" if (rec.read_dispatch_current_region(f) == target
                                     and rec.dispatch_daily_locked(f))
                       else None),
            timeout=4.0, interval=0.20, desc=f"选择派遣地区 {target}")
        if not selected:
            ctx.log(f"宠物派遣:点击后 {target} 未进入可派遣状态")
            return "fail"
        if selected == "locked":
            ctx.log(f"宠物派遣:{target} 今日已派遣,检查下一槽")
            return "locked"
        ctx.log(f"宠物派遣:已选择地区 {target}")
        return "ready"

    def _dispatch_current(self, target: str, slot_idx: int) -> str:
        '派遣当前可用槽；返回 sent/locked/nopets/fail。'
        ctx = self.ctx
        
        
        if not self._select_slot(slot_idx):
            dev_log(f"[daily dispatch] 派遣前重新选择槽 {slot_idx + 1} 失败")
            return "fail"
        region_state = self._select_region(target)
        if region_state != "ready":
            return region_state
        frame = ctx.grab()
        if frame is None:
            return "fail"
        before = rec.read_dispatch_slots(frame)
        busy_before = sum(state == "busy" for state in before)
        if not self._open_member_drawer():
            dev_log(f"[daily dispatch] 打开成员抽屉失败 slots={before} "
                    f"button={rec.read_dispatch_button(frame)[0]}")
            return "fail"

        selected = self._select_available_pets()
        if selected < 0:
            ctx.log("宠物派遣:存在可选成员，但成员选择未确认成功")
            ctx.click(R.PT_MEMBER_CLOSE)
            ctx.wait_until(
                lambda f: not rec.member_drawer_open(f),
                timeout=3.0, desc="关闭选择成员")
            return "fail"
        if selected == 0:
            ctx.log("宠物派遣:没有空心圆或已选宠物,结束派遣")
            ctx.click(R.PT_MEMBER_CLOSE)
            ctx.wait_until(
                lambda f: not rec.member_drawer_open(f),
                timeout=3.0, desc="关闭选择成员")
            return "no_pets"

        ctx.log(f"宠物派遣:已选 {selected} 只宠物,执行派遣")
        if not ctx.press("f"):
            return "fail"

        def _sent_to_target_slot(frame) -> bool:
            slots = rec.read_dispatch_slots(frame)
            if slot_idx < len(slots) and slots[slot_idx] == "busy":
                return True
            
            
            return (
                rec.read_dispatch_current_region(frame) == target
                and rec.read_dispatch_button(frame)[0] == "recall"
            )

        def _after_go(frame):
            if rec.dispatch_confirm_dialog(frame)[0]:
                return "confirm"
            if not rec.member_drawer_open(frame) and _sent_to_target_slot(frame):
                return "sent"
            return None

        after = ctx.wait_until(_after_go, timeout=6.0, interval=0.2, desc="提交宠物派遣")
        if after == "confirm":
            frame = ctx.grab()
            if frame is None:
                return "fail"
            in_dialog, confirm_pt = rec.dispatch_confirm_dialog(frame)
            if not in_dialog or not ctx.click(confirm_pt or R.PT_DIALOG_CONFIRM):
                return "fail"
            sent = ctx.wait_until(
                lambda f: (not rec.dispatch_confirm_dialog(f)[0]
                           and _sent_to_target_slot(f)),
                timeout=8.0, interval=0.25, desc="确认少成员派遣")
            dev_log(f"[daily dispatch] 少成员确认后派遣结果={bool(sent)}")
            return "sent" if sent else "fail"
        dev_log(f"[daily dispatch] 派遣提交结果={after} target_slot={slot_idx + 1} "
                f"before_slots={before} busy_before={busy_before}")
        return "sent" if after == "sent" else "fail"

    def _select_slot(self, idx: int) -> bool:
        '选择左侧槽并等待界面刷新。'
        ctx = self.ctx
        if not ctx.click(R.PT_SLOT[idx]):
            return False
        ctx.sleep(0.30)
        frame = ctx.grab()
        return bool(frame is not None and rec.in_dispatch_page(frame))

    def _scan_dispatched(self, frame) -> set[str]:
        '只读取当前已在派遣或今日锁定的槽，不把“已完成”旧队伍当作今日已派。'
        dispatched: set[str] = set()
        current = rec.read_dispatch_current_region(frame)
        button = rec.read_dispatch_button(frame)[0]
        if current and (button == "recall" or rec.dispatch_daily_locked(frame)):
            dispatched.add(current)
        return dispatched

    def _scan_busy_regions(self, slots) -> set[str] | None:
        '逐槽读取倒计时队伍地区，避免地图“已完成”标记错误占用今日配置。'
        dispatched: set[str] = set()
        for idx, state in enumerate(slots[:self.MAX_SLOTS]):
            if state != "busy":
                continue
            current = None
            for attempt in (1, 2, 3):
                if not self._select_slot(idx):
                    continue
                frame = self.ctx.grab()
                current = (
                    rec.read_dispatch_current_region(frame)
                    if frame is not None else None
                )
                if current:
                    break
                dev_log(f"[daily dispatch] 倒计时槽 {idx + 1} 地区漏读 "
                        f"attempt={attempt}/3")
                self.ctx.sleep(0.20)
            if not current:
                self.ctx.log(f"宠物派遣:无法确认第 {idx + 1} 个倒计时槽的地区")
                return None
            dispatched.add(current)
        return dispatched

    def _close_unknown_drawer(self) -> bool:
        frame = self.ctx.grab()
        if frame is None or not rec.member_drawer_open(frame):
            return frame is not None
        if not self.ctx.click(R.PT_MEMBER_CLOSE):
            return False
        return bool(self.ctx.wait_until(
            lambda f: not rec.member_drawer_open(f),
            timeout=3.0, interval=0.2, desc="恢复派遣地图"))

    def _resume_confirm_dialog(self) -> bool:
        '从少成员确认弹窗中断点继续，确认后回到可扫描的派遣页。'
        frame = self.ctx.grab()
        if frame is None:
            return False
        in_dialog, confirm_pt = rec.dispatch_confirm_dialog(frame)
        if not in_dialog:
            return True
        dev_log("[daily dispatch] 检测到遗留的少成员确认弹窗，先确认再重扫三个槽")
        if not self.ctx.click(confirm_pt or R.PT_DIALOG_CONFIRM):
            return False
        return bool(self.ctx.wait_until(
            lambda f: (not rec.dispatch_confirm_dialog(f)[0]
                       and rec.in_dispatch_page(f)),
            timeout=8.0, interval=0.25, desc="恢复少成员派遣确认"))

    def _dispatch_count_exhausted(self, count) -> bool:
        '今日次数为0需连续两帧确认，避免单帧OCR把可派遣槽提前跳过。'
        if count is None or count[0] > 0:
            return False
        self.ctx.sleep(0.20)
        frame = self.ctx.grab()
        second = rec.read_dispatch_count(frame) if frame is not None else None
        exhausted = second is not None and second[0] <= 0
        dev_log(f"[daily dispatch] 今日次数0复核 first={count} second={second} "
                f"confirmed={exhausted}")
        return exhausted

    def _run_page_state_machine(self) -> tuple[int, int, bool, bool]:
        '返回 (领取数,派出数,是否无宠物正常结束,是否发生流程错误)。'
        ctx = self.ctx
        claimed = 0
        sent = 0
        no_pets = False
        failed = False
        dispatched: set[str] = set()

        if not self._resume_confirm_dialog():
            return claimed, sent, no_pets, True
        if not self._close_unknown_drawer():
            return claimed, sent, no_pets, True

        first = ctx.grab()
        if first is None:
            return claimed, sent, no_pets, True
        if rec.reward_overlay(first):
            if not self._dismiss_reward():
                return claimed, sent, no_pets, True
            first = ctx.grab()
            if first is None:
                return claimed, sent, no_pets, True
        initial_slots = rec.read_dispatch_slots(first)[:self.MAX_SLOTS]
        busy_regions = self._scan_busy_regions(initial_slots)
        if busy_regions is None:
            return claimed, sent, no_pets, True
        dispatched.update(busy_regions)
        remaining_slots = set(range(self.MAX_SLOTS))
        unknown_scans = 0
        slot_unknown: dict[int, int] = {}
        step = 0
        while remaining_slots and step < 24:
            step += 1
            if ctx.should_stop():
                dev_log(f"[daily dispatch] 状态机停止 reason={ctx.stop_reason or 'requested'} "
                        f"remaining_slots={sorted(remaining_slots)}")
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
                frame = ctx.grab()
                if frame is None:
                    failed = True
                    break

            slots = rec.read_dispatch_slots(frame)[:self.MAX_SLOTS]
            dispatched.update(self._scan_dispatched(frame))
            count = rec.read_dispatch_count(frame)
            pending = [name for name in self.regions if name not in dispatched]
            done_slots = [
                idx for idx in sorted(remaining_slots)
                if idx < len(slots) and slots[idx] == "done"
            ]
            idle_slots = [
                idx for idx in sorted(remaining_slots)
                if idx < len(slots) and slots[idx] == "idle"
            ]
            terminal_slots = [
                idx for idx in sorted(remaining_slots)
                if idx < len(slots) and slots[idx] == "busy"
            ]
            for idx in terminal_slots:
                remaining_slots.discard(idx)
            candidates = sorted(
                done_slots + idle_slots,
                key=lambda i: (
                    slot_unknown.get(i, 0),
                    0 if i in done_slots else 1,
                    i,
                ),
            )
            dev_log(
                f"[daily dispatch] step={step} slots={slots} count={count} "
                f"remaining_slots={sorted(remaining_slots)} candidates={candidates} "
                f"configured={self.regions} dispatched={sorted(dispatched)} "
                f"pending={pending}"
            )
            if not candidates:
                if remaining_slots and all(
                        idx >= len(slots) or slots[idx] == "none"
                        for idx in remaining_slots):
                    if unknown_scans < 2:
                        unknown_scans += 1
                        ctx.sleep(0.20)
                        continue
                    ctx.log("宠物派遣:剩余槽连续无法识别，停止并标记失败")
                    dev_log(f"[daily dispatch] 剩余槽连续无法识别 "
                            f"remaining_slots={sorted(remaining_slots)}")
                    failed = True
                    break
                remaining_slots.clear()
                break
            unknown_scans = 0
            idx = candidates[0]
            slot_state = slots[idx]
            if not self._select_slot(idx):
                ctx.log(f"宠物派遣:无法选择槽 {idx + 1},停止")
                failed = True
                break

            frame = ctx.grab()
            if frame is None:
                failed = True
                break
            state, point = rec.read_dispatch_button(frame)
            current = rec.read_dispatch_current_region(frame)
            locked = rec.dispatch_daily_locked(frame)
            if current and (state == "recall" or locked):
                dispatched.add(current)
            dev_log(f"[daily dispatch] 已选槽={idx + 1} slot_state={slot_state} "
                    f"button={state} current={current} locked={locked}")

            
            if state == "claim":
                claim_state = self._claim_current(point)
                if claim_state == "fail":
                    ctx.log(f"宠物派遣:领取第 {idx + 1} 槽失败,停止")
                    failed = True
                    break
                claimed += 1
                slot_unknown.pop(idx, None)
                if claim_state == "locked":
                    remaining_slots.discard(idx)
                    continue
                
                state = "member"
            elif locked:
                ctx.log(
                    f"宠物派遣:第 {idx + 1} 槽右下角请明日再来，"
                    "本槽已检查，继续下一槽")
                remaining_slots.discard(idx)
                slot_unknown.pop(idx, None)
                continue
            elif state not in ("member", "go"):
                retries = slot_unknown.get(idx, 0) + 1
                slot_unknown[idx] = retries
                dev_log(
                    f"[daily dispatch] 第{idx + 1}槽状态暂不一致 "
                    f"left={slot_state} button={state} retry={retries}/"
                    f"{self.SLOT_UNKNOWN_RETRIES}")
                if retries >= self.SLOT_UNKNOWN_RETRIES:
                    ctx.log(f"宠物派遣:第 {idx + 1} 槽连续无法确认可执行按钮")
                    failed = True
                    break
                ctx.sleep(0.20)
                continue
            else:
                slot_unknown.pop(idx, None)

            frame = ctx.grab()
            if frame is None:
                failed = True
                break
            dispatched.update(self._scan_dispatched(frame))
            count = rec.read_dispatch_count(frame)
            pending = [name for name in self.regions if name not in dispatched]
            if not pending:
                ctx.log("宠物派遣:配置地区均已派遣，本槽无需重复派遣")
                remaining_slots.discard(idx)
                continue
            
            if self._dispatch_count_exhausted(count):
                ctx.log("宠物派遣:今日剩余派遣次数为0,本槽检查完成")
                remaining_slots.discard(idx)
                continue

            current = rec.read_dispatch_current_region(frame)
            target = current if current in pending else pending[0]
            result = self._dispatch_current(target, idx)
            if result == "sent":
                sent += 1
                dispatched.add(target)
                remaining_slots.discard(idx)
                continue
            if result == "locked":
                dispatched.add(target)
                ctx.log(f"宠物派遣:{target} 今日已派遣，当前槽重新选择其它地区")
                continue
            if result == "no_pets":
                no_pets = True
                break
            ctx.log(f"宠物派遣:第 {idx + 1} 槽派遣失败,停止")
            failed = True
            break

        if step >= 24 and remaining_slots and not failed and not no_pets:
            failed = True
            dev_log(f"[daily dispatch] 状态步骤达到上限 remaining_slots="
                    f"{sorted(remaining_slots)}")

        remaining = [name for name in self.regions if name not in dispatched]
        dev_log(f"[daily dispatch] 结束 configured={self.regions} "
                f"dispatched={sorted(dispatched)} remaining={remaining}")

        return claimed, sent, no_pets, failed

    def run(self) -> str:
        ctx = self.ctx
        if not self._goto_dispatch_page():
            return TaskResult.FAIL
        claimed, sent, no_pets, failed = self._run_page_state_machine()
        ctx.log(
            f"宠物派遣:领取 {claimed} 个,派出 {sent} 个"
            + (",无可用宠物" if no_pets else ""))
        
        
        nav.back_to_world(ctx)
        if ctx.should_stop():
            return TaskResult.ABORT
        return TaskResult.FAIL if failed else TaskResult.SUCCESS
