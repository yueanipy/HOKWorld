'好友家浇水:进入家园 → 8 打开好友 → 选蓝色水壶房屋 → 碎步找田 → 累计成功两次。'
from __future__ import annotations

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult
from runtime_guard import dev_log


class FriendsTask(DailyTask):
    task_id = "friends"
    name = "好友家浇水"

    TARGET_SUCCESSES = 2
    MAX_FRIENDS = 12
    MAX_SCROLLS = 5
    MAX_WALK_STEPS = 20
    WALK_STEP_S = 0.12
    WALK_SETTLE_S = 0.28
    ACTION_SETTLE_S = 3.0
    PANEL_BADGE_WAIT_S = 1.5
    SCROLL_DURATION_S = 1.30
    SCROLL_SETTLE_S = 0.9

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        try:
            from daily.config import DailyConfig

            cfg = DailyConfig()
            self.MAX_FRIENDS = int(cfg.param(self.task_id, "max_friends", self.MAX_FRIENDS))
            configured_steps = int(cfg.param(self.task_id, "max_walk_steps", self.MAX_WALK_STEPS))
            
            self.MAX_WALK_STEPS = max(1, min(20, configured_steps))
        except Exception:
            pass

    def _ensure_homeland(self) -> bool:
        '不在任意家园时,通过管理地图的农贸作物节点传送回自家家园。'
        ctx = self.ctx
        frame = ctx.grab()
        if frame is not None and rec.homeland_loaded(frame):
            return True
        ctx.log("好友浇水:当前不在家园,通过农贸作物节点进入家园")
        if not nav.enter_manage_map(ctx):
            return False
        if not nav.teleport_via_node(ctx, R.PT_NODE_FARM):
            return False
        loaded = ctx.wait_until(rec.homeland_loaded, timeout=15.0, interval=0.4,
                                desc="进入自家家园")
        if loaded:
            ctx.sleep(0.8)
        return bool(loaded)

    def _open_panel(self) -> bool:
        ctx = self.ctx
        frame = ctx.grab()
        if frame is not None and rec.friend_panel_open(frame):
            return True
        if not ctx.press("8"):
            return False
        return bool(ctx.wait_until(rec.friend_panel_open, timeout=6.0, interval=0.3,
                                   desc="按8打开家园好友"))

    def _next_water_friend(self, visited: set[str]):
        '先稳定识别当前页；只有没有未访问水壶按钮时才慢速拖动列表。'
        ctx = self.ctx
        for page in range(self.MAX_SCROLLS + 1):
            if ctx.should_stop():
                return None
            
            frame = None
            houses = []
            rounds = max(1, int(self.PANEL_BADGE_WAIT_S / 0.25))
            for scan in range(rounds):
                frame = ctx.grab()
                houses = rec.friend_water_houses(frame) if frame is not None else []
                available = [house for house in houses if house["name"] not in visited]
                if available:
                    ctx.log(f"好友浇水:当前页识别到水壶按钮,选择 {available[0]['name']},不滑动")
                    return available[0]
                if frame is not None and rec.friend_list_empty(frame):
                    ctx.log("好友浇水:好友列表为空,直接停止")
                    return None
                if scan + 1 < rounds:
                    ctx.sleep(0.25)
            if frame is not None and not houses and rec.friend_list_empty(frame):
                ctx.log("好友浇水:好友列表为空,直接停止")
                return None
            if page >= self.MAX_SCROLLS:
                break
            ctx.log(f"好友浇水:当前页复核后没有未访问水壶按钮,慢速向下翻页 {page + 1}/{self.MAX_SCROLLS}")
            if not ctx.drag(R.PT_FRIEND_LIST_DRAG_FROM, R.PT_FRIEND_LIST_DRAG_TO,
                            self.SCROLL_DURATION_S):
                return None
            ctx.sleep(self.SCROLL_SETTLE_S)
            after = ctx.grab()
            change = rec.friend_list_change_score(frame, after) if frame is not None and after is not None else 0.0
            dev_log(f"[daily] 好友浇水: 好友列表拖动变化分={change:.2f}")
            if change < 2.0:
                ctx.log("好友浇水:下划后没有出现新好友(列表已到底),直接停止")
                return None
        return None

    def _enter_friend_home(self, friend: dict) -> bool:
        '点击蓝壶房屋并确认,经历加载后以“××的居所 + 右下1行”确认到达。'
        ctx = self.ctx
        if not ctx.click(friend["pt"]):
            return False
        dialog = ctx.wait_until(lambda frame: rec.teleport_dialog(frame)[0], timeout=5.0,
                                interval=0.3, desc=f"前往好友{friend['name']}确认")
        if not dialog:
            return False
        frame = ctx.grab()
        in_dialog, body, confirm = rec.teleport_dialog(frame) if frame is not None else (False, "", None)
        if not in_dialog or ("好友" not in body and "浇水" not in body and "前往" not in body):
            ctx.log(f"好友浇水:{friend['name']}弹窗内容异常,跳过({body!r})")
            ctx.press("esc")
            return False
        if not ctx.click(confirm or R.PT_DIALOG_CONFIRM):
            return False

        
        left_old_home = ctx.wait_until(lambda frame: not rec.homeland_loaded(frame), timeout=7.0,
                                       interval=0.25, desc="离开原家园")
        if not left_old_home:
            dev_log(f"[daily] 好友浇水: {friend['name']} 未检测到加载过渡")
            return False
        ready = ctx.wait_until(rec.friend_home_ready, timeout=18.0, interval=0.4,
                               desc="好友家加载完成(居所标题+1行)")
        if ready:
            ctx.sleep(0.6)
        return bool(ready)

    def _water_ready(self, first_frame) -> bool:
        '严格门槛:脚下有框、右下是水壶,并确认金环发生峰谷闪烁。'
        ctx = self.ctx
        if first_frame is None or rec.plot_frame_state(first_frame, R.ROI_PLOT_FEET_TIGHT) is None:
            return False
        water_score = 0.0
        harvest_score = 0.0
        ring_values = []
        for i in range(6):
            frame = first_frame if i == 0 else ctx.grab()
            if frame is not None:
                water_score = max(water_score, rec.water_action_score(frame))
                harvest_score = max(harvest_score, rec.harvest_action_score(frame))
                ring_values.append(rec.action_ring_gold_px(frame))
            if i < 5:
                ctx.sleep(0.15)
        ring_peak = max(ring_values) if ring_values else 0
        ring_low = min(ring_values) if ring_values else 0
        blinking = ring_peak >= rec.ACTION_RING_MIN and ring_low <= ring_peak * 0.45
        
        ready = (water_score >= rec.WATER_ACTION_TH
                 and water_score >= harvest_score and blinking)
        if ready:
            dev_log(
                f"[daily] 好友浇水: 脚下有框且闪烁水壶可用"
                f"(水壶{water_score:.2f}/镰刀{harvest_score:.2f}/环峰{ring_peak}/环谷{ring_low})")
        elif harvest_score >= rec.HARVEST_ACTION_TH and harvest_score > water_score and blinking:
            dev_log(
                f"[daily] 好友浇水: 识别到收割状态(水壶{water_score:.2f}/镰刀{harvest_score:.2f})"
                " → 当前版本不收割、不点击")
        return ready

    def _walk_and_water(self, needed: int) -> int:
        '向前碎步搜索；以水壶停止闪烁且图标主体变化确认一次成功。'
        ctx = self.ctx
        successes = 0

        for step in range(self.MAX_WALK_STEPS + 1):
            if ctx.should_stop():
                return successes
            frame = ctx.grab()
            if self._water_ready(frame):
                before_icon = rec.action_icon_signature(frame)
                if not ctx.click(R.PT_PLOT_FEET):
                    return successes
                
                ctx.sleep(self.ACTION_SETTLE_S)
                after = ctx.grab()
                icon_change = rec.action_icon_change_score(before_icon, after)
                still_water_ready = self._water_ready(after)
                changed = icon_change >= rec.ACTION_ICON_CHANGE_TH
                if not still_water_ready and changed:
                    successes += 1
                    ctx.log(
                        f"好友浇水:确认水壶停止闪烁且图标已变化(变化率{icon_change:.3f}),"
                        f"本好友成功 {successes} 次,"
                        f"本任务还需 {max(0, needed - successes)} 次")
                    if successes >= needed:
                        return successes
                else:
                    
                    ctx.log(
                        f"好友浇水:点击后状态转换未确认"
                        f"(仍为闪烁水壶={still_water_ready},图标变化率={icon_change:.3f}),"
                        f"本次不计数,继续当前好友搜索({step}/{self.MAX_WALK_STEPS}步)")
            if step >= self.MAX_WALK_STEPS:
                break
            ctx.tap("w", self.WALK_STEP_S)
            ctx.sleep(self.WALK_SETTLE_S)
        ctx.log(f"好友浇水:走满 {self.MAX_WALK_STEPS} 个碎步,本好友确认成功 {successes} 次")
        return successes

    def _return_home(self) -> bool:
        '关闭可能残留的好友面板,按 7 返回自家并稳定确认。'
        ctx = self.ctx
        from runtime_guard import dev_log

        frame = ctx.grab()
        if frame is not None and rec.friend_panel_open(frame):
            ctx.press("esc")
            ctx.wait_until(lambda fr: not rec.friend_panel_open(fr), timeout=3.0, interval=0.25)
            frame = ctx.grab()
        
        if frame is not None and (rec.in_own_home(frame) or not rec.in_friend_home(frame)):
            return True
        if not ctx.press("7"):
            return False
        
        ctx.sleep(1.0)
        left = ctx.wait_until(lambda fr: not rec.homeland_loaded(fr), timeout=7.0, interval=0.25,
                              desc="离开好友家")
        if not left:
            
            dev_log("[daily] 好友浇水: 未采到离开好友家的黑屏帧，继续确认自家状态")

        stable = 0

        def _own_home_stable(current) -> bool:
            nonlocal stable
            if current is None:
                stable = 0
                return False
            own = rec.in_own_home(current)
            if not own and rec.homeland_loaded(current):
                
                own = not rec.in_friend_home(current)
            stable = stable + 1 if own else 0
            return stable >= 2

        return bool(ctx.wait_until(_own_home_stable, timeout=18.0, interval=0.4,
                                   desc="返回自家家园(连续2帧确认)"))

    def run(self) -> str:
        ctx = self.ctx
        if not self._ensure_homeland():
            return TaskResult.FAIL

        
        visited: set[str] = set()
        total_successes = 0
        attempts = 0
        while total_successes < self.TARGET_SUCCESSES and attempts < self.MAX_FRIENDS:
            if ctx.should_stop():
                return TaskResult.ABORT
            if not self._open_panel():
                break
            friend = self._next_water_friend(visited)
            if friend is None:
                ctx.log("好友浇水:好友列表及下方页面没有新的蓝色水壶好友")
                break

            
            visited.add(friend["name"])
            attempts += 1
            ctx.log(f"好友浇水:尝试第 {attempts} 位 {friend['name']}(累计成功 {total_successes}/2)")
            if not self._enter_friend_home(friend):
                continue
            gained = self._walk_and_water(self.TARGET_SUCCESSES - total_successes)
            total_successes += gained
            ctx.log(f"好友浇水:{friend['name']}确认成功 {gained} 次,累计 {total_successes}/2")

            
            if total_successes >= self.TARGET_SUCCESSES:
                returned = self._return_home()
                if returned:
                    ctx.log(f"好友浇水:累计成功 {total_successes} 次,已按7返回自家并结束任务")
                else:
                    
                    ctx.log(
                        f"好友浇水:累计成功 {total_successes} 次,按7后未确认自家状态；"
                        "浇水任务仍记为成功")
                return TaskResult.SUCCESS

        self._return_home()
        ctx.log(f"好友浇水:已访问 {len(visited)} 位,累计仅成功 {total_successes}/2")
        return TaskResult.FAIL
