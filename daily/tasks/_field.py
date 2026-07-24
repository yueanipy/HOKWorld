'田地类任务基类(农贸作物 / 培养箱 / 浇水共用)—— 按 20260710202445 演示(TPS 模式)标定。'
from __future__ import annotations

import time

import daily.recognizer as rec
from daily import navigation as nav
from daily import regions as R
from daily.base import DailyTask, TaskResult


class FieldTask(DailyTask):
    '子类需设:NODEPT(管理地图节点)、taskid/name,并按需覆盖 DO 与参数。'

    NODE_PT = None                 
    MAX_ROWS = 30                  
                                   
    SEED_NAME = ""                 
    ROW_RETRY = 4                  
    
    
    
    
    ACTION_BLINK_SAMPLES = 6       
    ACTION_BLINK_INTERVAL = 0.15   
    ACTION_BLINK_STD_MIN = 7.0     
    ACTION_SETTLE_S = 3.0          
    ACTION_EVIDENCE_TTL_S = 2.0    
    ACTION_RING_PREFILTER_RATIO = 0.33  
    POST_ACTION_RECHECKS = 2       
    POST_ACTION_RECHECK_S = 0.25
    POST_WATER_RECHECKS = 1        
    MAX_WATER_CYCLES_PER_PLOT = 3  
    MAX_ACTION_STEPS_PER_PLOT = 8  
    CONTINUE_AFTER_WATER = False   
    FIELD_EXIT_MISSES = 3          
    ACTION_KIND_WATER_TH = rec.WATER_ACTION_TH  
    GAP_TAPS = 5                   
    STEP_S = 0.386                 
                                   
    LATE_STEP_S = 0.25             
                                   
    LATE_STEP_FROM = 4             
    
    
    
    
    
    
    ARRIVE_SEQ = (("drag", 172),)  
                                   
                                   
                                   
    APPROACH_FIRST = 0.417         
    LEFT_TAPS = 3                  
    COL_TAP = 0.211                
    
    
    
    
    
    
    
    
    
    
    
    
    
    USE_MINISTEP_APPROACH = True   
    
    
    USE_MINISTEP_ADVANCE = True
    
    
    
    ONE_ROW_FLOW_PX = 12.0         
    MINI_STEP_S = 0.09             
    MINI_STEP_S = 0.09             
    MINI_SETTLE_S = 0.25           
    FRAME_RECHECK_S = 0.08         
    MINI_RECONFIRM_S = 0.30        
    MINI_MAX_ADVANCE = 30          
                                   
                                   
    MINI_MAX_APPROACH = 16         
    MINI_MIN_APPROACH = 4          
                                   
    MINI_MIN_STEPS = 2             
    DO_HARVEST = True
    DO_PLANT = True
    DO_WATER = True
    HANDLE_HIGH_VALUE_WARNING = False  

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._action_evidence: tuple[float, str] | None = None
        from runtime_guard import dev_log
        try:
            from daily.config import DailyConfig
            c = DailyConfig()
        except Exception as exc:
            dev_log(f"[daily] {self.task_id} 配置加载失败(用默认参数)", exc)
            return
        
        for key, apply in (("rows", self._cfg_rows), ("seed", self._cfg_seed),
                           ("arrive", self._cfg_arrive)):
            v = None
            try:
                v = c.param(self.task_id, key, None)
                if v is not None:
                    apply(v)
            except Exception as exc:
                dev_log(f"[daily] {self.task_id} 配置项 {key}={v!r} 解析失败(忽略该项)", exc)

    def _clear_action_evidence(self) -> None:
        self._action_evidence = None

    def _action_evidence_valid(self, expected_kind: str | None = None) -> bool:
        if self._action_evidence is None:
            return False
        captured_at, kind = self._action_evidence
        if time.monotonic() - captured_at > self.ACTION_EVIDENCE_TTL_S:
            self._action_evidence = None
            return False
        return expected_kind is None or kind == expected_kind

    def _consume_action_evidence(self, expected_kind: str) -> bool:
        '消费同一停步轮次的联合识别结果；动作开始后凭据立即失效。'
        valid = self._action_evidence_valid(expected_kind)
        self._action_evidence = None
        return valid

    def _post_water_followup(self, kind: str, same_state_checks: int) -> tuple[str, int]:
        '判断浇水后的同格状态，避免把水壶残影当成下一次浇水。'
        from runtime_guard import dev_log

        if kind in ("harvest", "plant"):
            dev_log(f"[daily] {self.name}: 浇水后同格切换为 {kind}，开启下一轮操作")
            return "new_cycle", 0
        if kind == "water" and same_state_checks < self.POST_WATER_RECHECKS:
            self.ctx.sleep(self.POST_ACTION_RECHECK_S)
            dev_log(f"[daily] {self.name}: 浇水后仍显示水壶，短复查后再决定是否前进")
            return "retry", same_state_checks + 1
        if kind == "water":
            dev_log(f"[daily] {self.name}: 浇水后水壶持续未切换，按残影处理并结束本格")
            return "finished", same_state_checks
        return "unknown", same_state_checks

    def _cfg_rows(self, v) -> None:
        self.MAX_ROWS = max(1, min(40, int(v)))    

    def _cfg_seed(self, v) -> None:
        self.SEED_NAME = str(v or "")


    def _cfg_arrive(self, v) -> None:
        'arrive 覆盖:空列表=明确"不要任何到位动作"。'
        seq = []
        for op, val in v:
            op, val = str(op), float(val)
            if op in ("w", "a", "s", "d"):
                val = max(0.0, min(val, 5.0))
            elif op == "drag":
                val = max(-2000.0, min(val, 2000.0))
            seq.append((op, val))
        self.ARRIVE_SEQ = tuple(seq)

    
    def _run_arrive_seq(self) -> None:
        '前置锚:默认仅 F11 重置位姿(确定起点,F7 与游戏快捷键冲突勿用)。'
        ctx = self.ctx
        ctx.log(f"{self.name}:到位序列 " + (" → ".join(f"{op}({v})" for op, v in self.ARRIVE_SEQ) or "(空)"))
        for op, v in self.ARRIVE_SEQ:
            if ctx.should_stop():
                return
            if op in ("f11", "f7"):          
                
                
                ctx.press(op)
                ctx.sleep(1.0)
            elif op == "drag":
                
                
                from runtime_guard import dev_log
                for attempt in (1, 2, 3):
                    pre = ctx.grab()
                    ok = ctx.drag_camera(int(v))
                    ctx.sleep(0.8)           
                    post = ctx.grab()
                    shift = self._scene_shift(pre, post)
                    if ok and shift >= 6.0:  
                        if attempt > 1:
                            dev_log(f"[daily] {self.name}: 转角第{attempt}次生效(画面差{shift:.1f})")
                        break
                    dev_log(f"[daily] {self.name}: 转角未生效(注入={ok} 画面差{shift:.1f})→ 等待后重试")
                    ctx.sleep(1.0)           
                else:
                    ctx.log(f"{self.name}:转角 3 次均未生效(加载过慢/窗口失焦?)")
            elif op in ("w", "a", "s", "d"):
                ctx.walk(op, float(v))
                ctx.sleep(0.2)
            else:
                ctx.log(f"到位序列:未知操作 {op!r},跳过")

    @staticmethod
    def _scene_shift(pre, post) -> float:
        '两帧中央区域灰度均值差(转视角→大变 >15;静止场景 <2;抓帧失败=0 视作未转)。'
        if pre is None or post is None:
            return 0.0
        import cv2
        a = rec.normalize(pre)
        b = rec.normalize(post)
        H, W = a.shape[:2]
        ca = cv2.cvtColor(a[H // 4:H * 3 // 4, W // 4:W * 3 // 4], cv2.COLOR_BGR2GRAY)
        cb = cv2.cvtColor(b[H // 4:H * 3 // 4, W // 4:W * 3 // 4], cv2.COLOR_BGR2GRAY)
        if ca.shape != cb.shape or ca.size == 0:
            return 0.0
        return float(cv2.absdiff(ca, cb).mean())

    def _on_plot(self) -> bool:
        '站在田上?——用紧脚下 ROI(20260712 用户拍板"识别范围缩减固定":蓝线门槛降 100 后,。'
        f = self.ctx.grab()
        return f is not None and rec.plot_frame_state(f, R.ROI_PLOT_FEET_TIGHT) is not None

    def _approach_field(self) -> bool:
        '传送后走到第一行(首步:碎步 or 单发,由 USEMINISTEPAPPROACH 切换)。'
        return self._approach_field_ministep() if self.USE_MINISTEP_APPROACH else self._approach_field_single()

    def _approach_field_ministep(self) -> bool:
        '碎步版首步:极短 W 反复点,每步近静止判框,踩上第一行即停(防惯性冲过头)。'
        ctx = self.ctx
        from runtime_guard import dev_log
        for i in range(self.MINI_MAX_APPROACH):
            if ctx.should_stop():
                return False
            
            
            if i >= self.MINI_MIN_APPROACH and self._on_plot():
                ctx.log(f"{self.name}:首步碎步 {i} 步踩上田块(第一行)")
                dev_log(f"[daily] {self.name}: 首步碎步 {i} 步踩上第一行(下限{self.MINI_MIN_APPROACH}/单步{self.MINI_STEP_S}s)")
                return True
            ctx.tap("w", self.MINI_STEP_S)
            ctx.sleep(self.MINI_SETTLE_S)             
        ctx.sleep(self.MINI_RECONFIRM_S)              
        if self._on_plot():
            ctx.log(f"{self.name}:首步碎步末尾复查踩上田块(第一行)")
            return True
        dev_log(f"[daily] {self.name}: 首步碎步 {self.MINI_MAX_APPROACH} 步仍未踩框")
        ctx.log(f"{self.name}:首步碎步走完仍无框 → 放弃本任务(调转角/MINI_MAX_APPROACH)")
        return False

    def _approach_field_single(self) -> bool:
        '传送后走到第一行——确定性单发(用户拍板:每个动作按录制时长只执行一次,。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if ctx.should_stop():
            return False
        ctx.tap("w", self.APPROACH_FIRST)
        ctx.sleep(0.9)                        
        if self._on_plot():
            ctx.log(f"{self.name}:首步 {self.APPROACH_FIRST}s 踩上田块(第一行)")
            return True
        dev_log(f"[daily] {self.name}: 首步 {self.APPROACH_FIRST}s 后未踩框(单发不补步)")
        ctx.log(f"{self.name}:首步后脚下无框 → 放弃本任务(微调首步时长或转角,勿靠补步)")
        return False

    STRAFE_TAP = 0.195             
    MICRO_TAP = 0.09               

    def _strafe_to_col1(self, max_taps: int = 8) -> None:
        '贴到第一列(最左侧田块):两段都带视觉确认,绝不盲退——。'
        ctx = self.ctx
        off = False
        for _ in range(max_taps):
            if ctx.should_stop():
                return
            ctx.tap("a", self.STRAFE_TAP)
            ctx.sleep(0.45)                   
            if not self._on_plot():
                off = True
                break
        if not off:
            return                            
        for _ in range(6):
            if ctx.should_stop():
                return
            ctx.tap("d", self.MICRO_TAP)
            ctx.sleep(0.45)
            if self._on_plot():
                return                        
        from runtime_guard import dev_log
        dev_log(f"[daily] {self.name}: 回位微步 6 次未踩回田块(列回位失败,就地继续)")

    def _ensure_row_mode(self) -> None:
        '传送后确保操作范围=「1行」(G 键循环 1格→4格→1行,最多按 2 次。'
        ctx = self.ctx
        from runtime_guard import dev_log
        for presses in range(3):                     
            if ctx.should_stop():
                return
            f = ctx.grab()
            t = rec.action_mode_text(f) if f is not None else ""
            if "行" in t:
                dev_log(f"[daily] {self.name}: 操作范围=1行(按G {presses}次,标签={t!r})")
                return
            if "格" not in t or presses == 2:        
                break
            dev_log(f"[daily] {self.name}: 操作范围≠1行(标签={t!r})→ 按 G 切换")
            ctx.press("g")
            ctx.sleep(0.6)                           
        dev_log(f"[daily] {self.name}: 操作范围未确认为1行(标签={t!r}),按当前模式继续")

    def _goto_field(self) -> bool:
        ctx = self.ctx
        if not nav.enter_manage_map(ctx):
            return False
        if not nav.teleport_via_node(ctx, self.NODE_PT):
            return False
        
        
        loaded = ctx.wait_until(rec.homeland_loaded, timeout=15.0, interval=0.5,
                                desc="传送完成(左上「居所」标题)")
        if not loaded:
            from runtime_guard import dev_log
            dev_log(f"[daily] {self.name}: 传送 15s 未见「居所」标题 → 到位失败")
            return False                     
        ctx.sleep(0.8)                       
        self._ensure_row_mode()              
        ctx.center_camera()                  
        self._run_arrive_seq()               
        if not self._approach_field():       
            return False
        return True                          

    
    def _ring_active(self, samples: int = 6, interval: float = 0.15) -> bool:
        '右下按钮是否真高亮(=可操作)。'
        ctx = self.ctx
        from runtime_guard import dev_log
        vals = []
        for _ in range(samples):
            if ctx.should_stop():
                return False
            f = ctx.grab()
            if f is not None:
                vals.append(rec.action_ring_gold_px(f))
            ctx.sleep(interval)
        if not vals:
            return False
        mx, mn = max(vals), min(vals)
        active = mx >= rec.ACTION_RING_MIN and mn <= mx * 0.45
        dev_log(f"[daily] {self.name}: 金环序列 峰{mx}/谷{mn}(阈{rec.ACTION_RING_MIN},闪={mn <= mx * 0.45})"
                f" → {'激活' if active else '灰暗'}")
        return active

    def _water_available(self) -> bool:
        '右下角是否"浇水壶"按钮(=当前田需浇)。'
        ctx = self.ctx
        for _ in range(2):
            if ctx.should_stop():
                return False
            f = ctx.grab()
            if f is not None and rec.water_action_available(f):
                return True
            ctx.sleep(0.12)
        return False

    def _water_here(self, wait_pot_s: float = 0.0) -> bool:
        '浇当前位置——需不需要浇看右下角浇水壶按钮(白壶模板),不看蓝框(20260711 用户拍板:。'
        ctx = self.ctx
        from runtime_guard import dev_log
        
        
        reused = wait_pot_s <= 0 and self._consume_action_evidence("water")
        if reused:
            pot = True
            active = True
            dev_log(f"[daily] {self.name}: 复用同轮water联合凭据，执行前不重复采样")
        elif wait_pot_s > 0:
            self._clear_action_evidence()
            pot = ctx.wait_until(rec.water_action_available, timeout=wait_pot_s,
                                 interval=0.25, desc="")   
            active = self._ring_active() if pot else False
        else:
            pot = self._water_available()
            active = self._ring_active() if pot else False
        if not pot:
            dev_log(f"[daily] {self.name}: 浇水跳过(右下无浇水壶按钮=无需浇,等待{wait_pot_s}s)")
            return False
        if not active:
            dev_log(f"[daily] {self.name}: 浇水跳过(壶在但**金环未激活**=不可浇,如倒计时中)")
            return False
        
        for attempt in range(self.ROW_RETRY):
            if ctx.should_stop():
                return False
            pt = R.PT_PLOT_FEET              
                                             
            before = ctx.grab()
            before_signature = rec.action_icon_signature(before) if before is not None else None
            dev_log(f"[daily] {self.name}: 浇水点击#{attempt + 1} pt={pt}(脚下点)")
            self._clear_action_evidence()
            ctx.click(pt)
            ctx.sleep(self.ACTION_SETTLE_S)  
            f2 = ctx.grab()
            if f2 is not None and rec.seed_panel_open(f2):
                
                ctx.log(f"{self.name}:点击误开种子面板 → ESC 关闭重试")
                dev_log(f"[daily] {self.name}: 浇水#{attempt + 1} 误开种子面板 → ESC 重试")
                ctx.press("esc")
                ctx.wait_until(lambda fr: not rec.seed_panel_open(fr),
                               timeout=3.0, interval=0.4, desc="种子面板关闭")
                continue
            
            if self._action_transition_confirmed("water", before_signature):
                dev_log(f"[daily] {self.name}: 浇水#{attempt + 1} 生效(图标状态已切换)")
                return True
            dev_log(f"[daily] {self.name}: 浇水#{attempt + 1} 点了但图标状态未切换 → 重试")
        ctx.log(f"{self.name}:重试 {self.ROW_RETRY} 次金环未熄 → 跳过此处(宁可漏不可错)")
        dev_log(f"[daily] {self.name}: 浇水重试 {self.ROW_RETRY} 次未浇上 → 放弃(能浇却没浇上)")
        return False

    def _harvest_here(self) -> bool:
        '收割当前行:右下镰刀按钮在(模板,只扫按钮小 ROI)→ 点「1行」整行收割 → 镰刀消失=完成。'
        ctx = self.ctx
        from runtime_guard import dev_log
        clicked = False
        for i in range(self.ROW_RETRY):
            if ctx.should_stop():
                return clicked
            reused = i == 0 and self._consume_action_evidence("harvest")
            if reused:
                allowed = True
                dev_log(f"[daily] {self.name}: 复用同轮harvest联合凭据，执行前不重复采样")
            else:
                f = ctx.grab()
                
                allowed = bool(f is not None and rec.harvest_action_available(f)
                               and self._ring_active())
            if not allowed:
                if i:
                    dev_log(f"[daily] {self.name}: 收割生效(金环熄灭,点击{i}次)")
                return clicked
            before = ctx.grab()
            before_signature = rec.action_icon_signature(before) if before is not None else None
            pt = R.PT_ACTION_ROW if i == 0 else R.PT_ACTION_GOLD   
            dev_log(f"[daily] {self.name}: 收割点击#{i + 1} {'「1行」' if i == 0 else '金按钮'}")
            self._clear_action_evidence()
            ctx.click(pt)
            clicked = True
            ctx.sleep(self.ACTION_SETTLE_S)  
            if self._action_transition_confirmed("harvest", before_signature):
                dev_log(f"[daily] {self.name}: 收割生效(图标状态已切换,点击{i + 1}次)")
                return True
        ctx.log(f"{self.name}:收割重试 {self.ROW_RETRY} 次金环未熄 → 跳过(宁可漏不可错)")
        dev_log(f"[daily] {self.name}: 收割重试 {self.ROW_RETRY} 次金环未熄 → 放弃")
        return clicked

    def _action_transition_confirmed(self, kind: str, before_signature) -> bool:
        '动作静默期结束后的快速三帧确认；只替代重复闪烁等待，不缩短动作静默。'
        ctx = self.ctx
        from runtime_guard import dev_log

        scores: list[float] = []
        changes: list[float] = []
        for sample in range(3):
            if ctx.should_stop():
                return False
            frame = ctx.grab()
            if frame is not None:
                score = (rec.water_action_score(frame) if kind == "water"
                         else rec.harvest_action_score(frame))
                scores.append(score)
                changes.append(rec.action_icon_change_score(before_signature, frame))
            if sample < 2:
                ctx.sleep(0.12)
        threshold = (self.ACTION_KIND_WATER_TH if kind == "water"
                     else rec.HARVEST_ACTION_TH)
        below_count = sum(score < threshold for score in scores)
        max_change = max(changes, default=0.0)
        confirmed = bool(scores) and (
            below_count >= min(2, len(scores))
            or max_change >= rec.ACTION_ICON_CHANGE_TH
        )
        dev_log(
            f"[daily] {self.name}: {kind}快速结果确认 分数="
            f"{','.join(f'{score:.2f}' for score in scores) or '无帧'} "
            f"低于阈值={below_count}/{len(scores)} 图标变化={max_change:.3f}/"
            f"{rec.ACTION_ICON_CHANGE_TH} → {'成功' if confirmed else '未确认'}"
        )
        return confirmed

    def _pick_seed_and_confirm(self) -> bool:
        '种子面板内:换页签找目标种子(文本识别)→ 点种子 → 点「选择」。'
        ctx = self.ctx
        
        
        for k, tx in enumerate(R.SEED_TAB_XS):
            if ctx.should_stop():
                return False
            if k == 0:
                continue                     
            ctx.click((tx, R.SEED_TAB_Y))
            ctx.sleep(0.5)
            f = ctx.grab()
            if f is None:
                return False
            if self.SEED_NAME:
                pt = rec.find_seed(f, self.SEED_NAME)
            else:
                names = rec.seed_names(f)    
                pt = names[0][1:] if names else None
            if pt:
                ctx.click(pt)
                ctx.sleep(0.4)
                ctx.click(R.PT_SEED_SELECT)          
                ctx.sleep(self.ACTION_SETTLE_S)      
                f2 = ctx.grab()
                warned, confirmed = self._handle_high_value_warning(f2)
                if warned:
                    return confirmed
                if f2 is not None and rec.seedling_insufficient(f2):
                    ctx.log("幼苗不足 → 我知道了(跳过种植,绝不自动购买)")
                    ctx.click(R.PT_DIALOG_CANCEL)    
                    ctx.sleep(0.5)
                    ctx.press("esc")                 
                    ctx.wait_until(lambda fr: not rec.seed_panel_open(fr),
                                   timeout=3.0, interval=0.4, desc="种子面板关闭")
                    return False
                return True
        ctx.log("种子面板未找到可选种子(限时页已按约定跳过)")
        ctx.press("esc")                             
        return False

    def _handle_high_value_warning(self, frame) -> tuple[bool, bool]:
        '处理农贸作物高售价提示；返回 (是否出现, 是否确认关闭)。'
        if not self.HANDLE_HIGH_VALUE_WARNING or frame is None:
            return False, False
        matched, no_remind_pt, confirm_pt = rec.farm_high_value_warning(frame)
        if not matched:
            return False, False

        ctx = self.ctx
        from runtime_guard import dev_log
        ctx.log("农贸作物:高售价作物提示 → 勾选今日不再提示并确定")
        dev_log("[daily] 农贸作物: 命中高售价作物二次确认")
        if not ctx.click(no_remind_pt):
            dev_log("[daily] 农贸作物: 点击今日不再提示圆圈失败")
            return True, False
        ctx.sleep(0.25)  
        if not ctx.click(confirm_pt):
            dev_log("[daily] 农贸作物: 点击高售价提示确定失败")
            return True, False
        ctx.sleep(self.ACTION_SETTLE_S)
        after = ctx.grab()
        if after is None:
            ctx.log("农贸作物:高售价提示确认后截图失败 → 停止本行种植")
            dev_log("[daily] 农贸作物: 高售价提示确认后无法截图验证")
            return True, False
        still_open = rec.farm_high_value_warning(after)[0]
        if still_open:
            ctx.log("农贸作物:高售价提示仍未关闭 → 停止本行种植")
            dev_log("[daily] 农贸作物: 高售价提示确认后仍存在")
            return True, False
        dev_log("[daily] 农贸作物: 高售价提示已确认关闭")
        return True, True

    def _dismiss_proficiency(self) -> bool:
        '「熟练度提升」全屏浮层出现则点空白关闭(收割/种植后随机弹。'
        ctx = self.ctx
        from runtime_guard import dev_log
        f = ctx.grab()
        if f is None or not rec.proficiency_overlay(f):
            return False
        ctx.log(f"{self.name}:「熟练度提升」浮层 → 点空白关闭")
        dev_log(f"[daily] {self.name}: 熟练度浮层出现 → 点击空白关闭")
        ctx.click(R.PT_PROF_DISMISS)
        ctx.wait_until(lambda fr: not rec.proficiency_overlay(fr),
                       timeout=3.0, interval=0.4, desc="熟练度浮层关闭")
        ctx.sleep(0.3)                               
        return True

    def _plant_here(self, check_prof: bool = False) -> tuple[bool, bool]:
        '种当前位置(按钮判定为"种植"时调用):点脚下空田 → 面板开 → 选种。'
        ctx = self.ctx
        from runtime_guard import dev_log
        f = ctx.grab()
        if f is not None and rec.seed_panel_open(f):
            self._clear_action_evidence()
            planted = self._pick_seed_and_confirm()  
            return planted, planted
        for attempt in (1, 2, 3):                    
            self._clear_action_evidence()
            if not ctx.click(R.PT_PLOT_FEET):        
                return False, True
            ctx.sleep(self.ACTION_SETTLE_S)          
            f2 = ctx.grab()
            if f2 is None:
                continue
            warned, confirmed = self._handle_high_value_warning(f2)
            if warned:
                return (True, True) if confirmed else (False, False)
            if rec.seed_panel_open(f2):
                planted = self._pick_seed_and_confirm()
                return planted, planted
            if rec.seedling_insufficient(f2):
                ctx.log("幼苗不足 → 我知道了(停止后续种植,绝不自动购买)")
                ctx.click(R.PT_DIALOG_CANCEL)
                ctx.sleep(0.5)
                return False, False
            water_score = rec.water_action_score(f2)
            plant_text = rec.plant_action_text(f2)
            if ("铲除" in plant_text
                    or (water_score >= self.ACTION_KIND_WATER_TH
                        and "更换" not in plant_text)):
                dev_log(f"[daily] {self.name}: 种植点击#{attempt} 生效"
                        f"(右下切换水壶={water_score:.2f},标签={plant_text!r})")
                return True, True
            
            
            if check_prof and self._dismiss_proficiency():
                dev_log(f"[daily] {self.name}: 种植点击#{attempt} 被熟练度浮层吞掉 → 已关闭,重试点田")
                continue
            dev_log(f"[daily] {self.name}: 种植点击#{attempt} 未确认状态翻转"
                    f"(水壶={water_score:.2f},标签={plant_text!r})")
        dev_log(f"[daily] {self.name}: 种植:3 次均未确认面板或水壶状态 → 本行不标记种植完成")
        return False, True

    
    def _advance_next_row(self) -> bool:
        '前进一行(换行:碎步 or 单发,由 USEMINISTEPADVANCE 切换)。'
        return self._advance_next_row_ministep() if self.USE_MINISTEP_ADVANCE else self._advance_next_row_single()

    def _advance_next_row_ministep(self) -> bool:
        '碎步换行(按钮激活即停,20260711 用户拍板,替代光流阈值——光流版实测仍跨行):。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if ctx.should_stop():
            return False
        self._clear_action_evidence()
        
        
        none_streak = 0
        for i in range(self.MINI_MAX_ADVANCE):   
            if ctx.should_stop():
                return False
            self._clear_action_evidence()         
            ctx.tap("w", self.MINI_STEP_S)
            cur = ctx.grab()
            
            state = rec.plot_frame_state(cur, R.ROI_PLOT_FEET_TIGHT) if cur is not None else None
            if state is None:
                
                ctx.sleep(self.FRAME_RECHECK_S)
                cur = ctx.grab()
                state = (rec.plot_frame_state(cur, R.ROI_PLOT_FEET_TIGHT)
                         if cur is not None else None)
            
            
            
            if state is not None and i + 1 >= self.MINI_MIN_STEPS:   
                none_streak = 0                  
                
                
                fast_gold = rec.action_ring_gold_px(cur)
                prefilter_min = max(1, int(rec.ACTION_RING_MIN * self.ACTION_RING_PREFILTER_RATIO))
                if fast_gold < prefilter_min:
                    continue
                
                
                dev_log(f"[daily] {self.name}: 碎步换行 {i + 1}步 金环预门={fast_gold}/{prefilter_min}")
                kind = self._action_kind()
                if kind is not None:
                    dev_log(f"[daily] {self.name}: 碎步换行 {i + 1}步 联合判定={kind}"
                            f"(框={state}) → 停下操作并缓存凭据")
                    return True
            
            if state is None:
                none_streak += 1
                dev_log(f"[daily] {self.name}: 碎步换行 第{i + 1}步紧脚框=None"
                        f" 连续无框={none_streak}/{self.FIELD_EXIT_MISSES}")
                if none_streak >= self.FIELD_EXIT_MISSES:
                    left = len(rec.water_bubbles(cur)) if cur is not None else 0
                    dev_log(f"[daily] {self.name}: 碎步换行 第{i + 1}步连续{none_streak}次无框且无高亮 → 田垄走完")
                    ctx.log(f"{self.name}:连续无框 → 田垄走完"
                            + (f"(仍见 {left} 气泡)" if left else ""))
                    return False
            else:
                none_streak = 0
        
        
        
        dev_log(f"[daily] {self.name}: 碎步换行达步数上限 {self.MINI_MAX_ADVANCE}步 → 直接结束本田")
        ctx.log(f"{self.name}:达 {self.MINI_MAX_ADVANCE} 步上限 → 结束本田任务")
        return False

    def _advance_next_row_ministep_flow(self) -> bool:
        '【留存·可回退】光流里程计版:累计地面竖直位移到 ONEROWFLOWPX 即停。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if ctx.should_stop():
            return False
        f = ctx.grab()
        if f is not None and rec.board_ahead(f):
            ctx.log(f"{self.name}:看见公告牌 → 行尾")
            return False
        ctx.sleep(0.5)                       
        prev = ctx.grab()
        acc = 0.0
        for i in range(self.MINI_MAX_ADVANCE):   
            if ctx.should_stop():
                return False
            ctx.tap("w", self.MINI_STEP_S)
            ctx.sleep(self.MINI_SETTLE_S)    
            cur = ctx.grab()
            dy = 0.0
            if prev is not None and cur is not None:
                dy = rec.ground_shift_dy(prev, cur)
                if dy > 0:                   
                    acc += dy
            prev = cur
            dev_log(f"[daily] {self.name}: 光流碎步 第{i + 1}步 dy={dy:+.1f} 累计{acc:.1f}px"
                    f"(阈{self.ONE_ROW_FLOW_PX})")   
            
            
            
            if acc >= self.ONE_ROW_FLOW_PX:
                state = rec.plot_frame_state(cur) if cur is not None else None
                if state is None:            
                    ctx.sleep(0.4)
                    cur = ctx.grab()
                    state = rec.plot_frame_state(cur) if cur is not None else None
                dev_log(f"[daily] {self.name}: 光流碎步换行 {i + 1}步 累计位移{acc:.1f}px"
                        f"(阈{self.ONE_ROW_FLOW_PX}) 分色={state}")
                if state is None:            
                    left = len(rec.water_bubbles(cur)) if cur is not None else 0
                    ctx.log(f"{self.name}:换行后脚下无框 → 田垄走完"
                            + (f"(仍见 {left} 气泡)" if left else ""))
                    return False
                return True                  
        
        dev_log(f"[daily] {self.name}: 光流碎步撞安全上限 {self.MINI_MAX_ADVANCE}步 仅累计{acc:.1f}px")
        ctx.log(f"{self.name}:换行撞安全上限 → 结束(疑卡住;查 ONE_ROW_FLOW_PX/MINI_STEP_S)")
        return False

    def _advance_next_row_ministep_gap(self) -> bool:
        '【已弃用·留存】旧"缝隙碎步":追踪脚下框"当前行(有)→沟(无)→下一行(有)"跳变。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if ctx.should_stop():
            return False
        f = ctx.grab()
        if f is not None and rec.board_ahead(f):
            ctx.log(f"{self.name}:看见公告牌 → 行尾")
            return False
        ctx.sleep(0.5)                       
        seen_gap = False
        marks = []                           
        for i in range(self.MINI_MAX_ADVANCE):
            if ctx.should_stop():
                return False
            ctx.tap("w", self.MINI_STEP_S)
            ctx.sleep(self.MINI_SETTLE_S)    
            f = ctx.grab()
            present = rec.plot_frame_present(f) if f is not None else False
            if not present:
                seen_gap = True              
                marks.append(".")
                continue
            if not (seen_gap and i + 1 >= self.MINI_MIN_STEPS):
                marks.append("=")            
                continue
            
            marks.append("^")
            ctx.sleep(self.MINI_RECONFIRM_S)  
            st2 = None
            f2 = ctx.grab()
            if f2 is not None:
                st2 = rec.plot_frame_state(f2)
            dev_log(f"[daily] {self.name}: 碎步换行到下一行 轨迹[{''.join(marks)}] {i + 1}步 分色={st2}")
            return True
        left = len(rec.water_bubbles(f)) if f is not None else 0
        if seen_gap:                          
            ctx.log(f"{self.name}:碎步过沟后无下一行框 → 浇水结束"
                    + (f"(画面仍见 {left} 个水壶气泡,未处理)" if left else ""))
            dev_log(f"[daily] {self.name}: 碎步换行结束 轨迹[{''.join(marks)}]({self.MINI_MAX_ADVANCE}步用尽)")
        else:                                 
            dev_log(f"[daily] {self.name}: 碎步 {self.MINI_MAX_ADVANCE} 步未离开当前行 轨迹[{''.join(marks)}](疑卡住)")
            ctx.log(f"{self.name}:碎步换行未推进 → 结束(检查 MINI_STEP_S/MINI_MAX_ADVANCE)")
        return False

    def _advance_next_row_single(self) -> bool:
        '前进一行——确定性单发(用户拍板:W 0.24s 一次=一行,演示第2→3行实测。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if ctx.should_stop():
            return False
        f = ctx.grab()
        if f is not None and rec.board_ahead(f):
            ctx.log(f"{self.name}:看见公告牌 → 行尾")
            return False
        ctx.sleep(0.5)                       
        self._adv_w_count = getattr(self, "_adv_w_count", 0) + 1   
        step_s = self.LATE_STEP_S if self._adv_w_count >= self.LATE_STEP_FROM else self.STEP_S
        ctx.tap("w", step_s)                 
        dev_log(f"[daily] {self.name}: 第{self._adv_w_count}次换行 W={step_s}s")
        ctx.sleep(0.9)                       
        f = ctx.grab()
        state = rec.plot_frame_state(f) if f is not None else None
        if state is None:
            
            ctx.sleep(0.4)
            f = ctx.grab()
            state = rec.plot_frame_state(f) if f is not None else None
            if state is not None:
                dev_log(f"[daily] {self.name}: 前进一行复查见框 state={state}(首查漏检)→ 继续")
        if state is not None:
            dev_log(f"[daily] {self.name}: 前进一行后 state={state} → 继续")
            return True
        left = len(rec.water_bubbles(f)) if f is not None else 0
        ctx.log(f"{self.name}:前进一行后脚下无框 → 浇水结束"
                + (f"(画面仍见 {left} 个水壶气泡,未处理)" if left else ""))
        return False

    def _left_to_col1(self) -> None:
        '踩上第一行后:A 左移固定 3 次(0.195s/次,单发不补步)到最左格(用户拍板:。'
        ctx = self.ctx
        from runtime_guard import dev_log
        self._clear_action_evidence()
        for i in range(self.LEFT_TAPS):
            if ctx.should_stop():
                return
            ctx.tap("a", self.COL_TAP)
            ctx.sleep(0.7)                           
        dev_log(f"[daily] {self.name}: A×{self.LEFT_TAPS} 到最左格(脚下框={self._on_plot()})")

    
    def _action_blinking(self, samples: int | None = None, interval: float | None = None,
                         quiet: bool = False) -> bool:
        '右下动作按钮是否在闪烁(=可操作:浇/收/种)。'
        ctx = self.ctx
        import numpy as np
        from runtime_guard import dev_log
        n = samples or self.ACTION_BLINK_SAMPLES
        iv = interval or self.ACTION_BLINK_INTERVAL
        grays = []
        for _ in range(n):
            if ctx.should_stop():
                return False
            f = ctx.grab()
            if f is not None:
                g = rec.action_roi_gray(f)
                if g is not None:
                    grays.append(g.astype(np.float32))
            ctx.sleep(iv)
        if len(grays) < 3 or len({g.shape for g in grays}) != 1:
            return not quiet                         
        std = float(np.stack(grays).std(axis=0).mean())
        active = std >= self.ACTION_BLINK_STD_MIN
        if not quiet:
            dev_log(f"[daily] {self.name}: 动作按钮 闪烁std={std:.1f}(阈{self.ACTION_BLINK_STD_MIN})"
                    f" → {'可操作' if active else '灰暗跳过'}")
        return active

    def _action_kind(self, *, expect_plant: bool = False):
        '脚下有框后判右下动作：模板识别水/镰，小 ROI 文字正向识别种植。'
        ctx = self.ctx
        from runtime_guard import dev_log
        if self._action_evidence_valid():
            kind = self._action_evidence[1]
            dev_log(f"[daily] {self.name}: 复用停步同轮{kind}凭据，跳过重复金环/图标采样")
            return kind
        self._action_evidence = None
        
        
        f0 = ctx.grab()
        if f0 is None or rec.plot_frame_state(f0, R.ROI_PLOT_FEET_TIGHT) is None:
            ctx.sleep(0.3)
            f0 = ctx.grab()
            if f0 is None or rec.plot_frame_state(f0, R.ROI_PLOT_FEET_TIGHT) is None:
                dev_log(f"[daily] {self.name}: 行判定 kind=None(脚下无框=不在田上)")
                return None
        
        
        
        
        ring, ws, hs = [], 0.0, 0.0
        peak_frame, peak_ring = None, -1
        for _ in range(6):                   
            if ctx.should_stop():
                return None
            f = ctx.grab()
            if f is not None:
                ws = max(ws, rec.water_action_score(f))     
                hs = max(hs, rec.harvest_action_score(f))
                gold = rec.action_ring_gold_px(f)
                ring.append(gold)
                if gold > peak_ring:
                    peak_frame, peak_ring = f, gold
            ctx.sleep(0.15)
        mx = max(ring) if ring else 0
        mn = min(ring) if ring else 0
        active = mx >= rec.ACTION_RING_MIN and mn <= mx * 0.45   
        
        
        clear_water = (active and ws >= max(self.ACTION_KIND_WATER_TH, 0.90)
                       and ws >= hs + 0.12)
        clear_harvest = (active and hs >= max(rec.HARVEST_ACTION_TH + 0.18, 0.85)
                         and hs >= ws + 0.12)
        plant_text = (rec.plant_action_text(peak_frame)
                      if active and self.DO_PLANT and peak_frame is not None
                      and not clear_water and not clear_harvest else "")
        plant_label = "更换" in plant_text
        if not active:
            kind = None                      
        elif clear_water:
            kind = "water"
        elif clear_harvest:
            kind = "harvest"
        elif plant_label:
            kind = "plant"                  
        elif (expect_plant and hs < rec.HARVEST_ACTION_TH
              and ws < self.ACTION_KIND_WATER_TH):
            kind = "plant"                  
        elif ws >= self.ACTION_KIND_WATER_TH and ws >= hs:
            kind = "water"                   
        elif hs >= rec.HARVEST_ACTION_TH:
            kind = "harvest"
        else:
            kind = "plant"                   
        dev_log(f"[daily] {self.name}: 行判定 kind={kind}(环峰{mx}/谷{mn}/阈{rec.ACTION_RING_MIN}"
                f" 水分{ws:.2f}/分类阈{self.ACTION_KIND_WATER_TH}"
                f" 镰分{hs:.2f}/阈{rec.HARVEST_ACTION_TH}"
                f" 种植标签={plant_text!r} 收割后待种={expect_plant})")
        self._action_evidence = (time.monotonic(), kind) if kind else None
        return kind

    def run(self) -> str:
        from runtime_guard import dev_log

        ctx = self.ctx
        if not self._goto_field():
            
            if ctx.should_stop():
                return TaskResult.ABORT
            ctx.log(f"{self.name}:到位失败 → 重新传送自愈重试一次")
            dev_log(f"[daily] {self.name}: 到位失败 → 重新传送自愈重试")
            if not self._goto_field():
                ctx.log(f"{self.name}:重试后仍到位失败 → 放弃本任务")
                return TaskResult.FAIL
        self._left_to_col1()                         
        can_plant = self.DO_PLANT
        self._adv_w_count = 0                         
        for row in range(1, self.MAX_ROWS + 1):
            if ctx.should_stop():
                return TaskResult.ABORT
            ctx.log(f"{self.name}:第 {row} 行(上限 {self.MAX_ROWS})")
            
            
            done_ops: set = set()
            did_any, none_waits, harvest_clicked = False, 0, False
            after_water = False
            water_cycles = 0
            same_water_checks = 0
            for _ in range(self.MAX_ACTION_STEPS_PER_PLOT):
                if ctx.should_stop():
                    return TaskResult.ABORT
                expect_plant = (can_plant and "harvest" in done_ops and "plant" not in done_ops)
                kind = self._action_kind(expect_plant=expect_plant)
                if kind is None:
                    
                    
                    if harvest_clicked and self._dismiss_proficiency():
                        continue
                    
                    
                    recheck_limit = (self.POST_WATER_RECHECKS if after_water
                                     else self.POST_ACTION_RECHECKS)
                    if did_any and none_waits < recheck_limit:
                        none_waits += 1
                        ctx.sleep(self.POST_ACTION_RECHECK_S)
                        continue
                    if not did_any:
                        ctx.log(f"{self.name}:第 {row} 行 灰暗不可操作 → 跳过")
                    break                            
                none_waits = 0
                if after_water:
                    decision, same_water_checks = self._post_water_followup(
                        kind, same_water_checks)
                    if decision == "retry":
                        continue
                    if decision == "finished":
                        break
                    if decision == "new_cycle":
                        done_ops.clear()
                        harvest_clicked = False
                        after_water = False
                if kind == "harvest":
                    if not self.DO_HARVEST or "harvest" in done_ops:
                        break                        
                    harvest_clicked = self._harvest_here()   
                    if not harvest_clicked:
                        ctx.log(f"{self.name}:第 {row} 行收割未实际点击，未标记完成，原地重新识别")
                        continue
                    done_ops.add("harvest")
                    did_any = True
                    ctx.sleep(0.5)                   
                    self._dismiss_proficiency()      
                    continue                         
                if kind == "plant":
                    if not can_plant or "plant" in done_ops:
                        break
                    planted, can_plant = self._plant_here(check_prof=harvest_clicked)
                    if not planted:
                        ctx.log(f"{self.name}:第 {row} 行未确认种植成功，不标记完成")
                        break
                    done_ops.add("plant")
                    did_any = True
                    continue                         
                if self.DO_WATER:
                    if self._water_here():
                        did_any = True
                        if not self.CONTINUE_AFTER_WATER:
                            break
                        water_cycles += 1
                        if water_cycles >= self.MAX_WATER_CYCLES_PER_PLOT:
                            ctx.log(f"{self.name}:第 {row} 行达到同格浇水循环上限"
                                    f" {self.MAX_WATER_CYCLES_PER_PLOT} → 前进")
                            break
                        after_water = True
                        same_water_checks = 0
                        continue
                    dev_log(f"[daily] {self.name}: water 候选经二次复查不成立，回到行状态机重新分类")
                    ctx.sleep(0.25)
                    continue
                break
            if row >= self.MAX_ROWS:
                ctx.log(f"{self.name}:达到行数上限 {self.MAX_ROWS}(W 已走 {row - 1} 次)→ 结束")
                break
            if not self._advance_next_row():
                ctx.log(f"{self.name}:共处理 {row} 行,结束")
                break
        nav.back_to_world(ctx)
        
        return TaskResult.ABORT if ctx.should_stop() else TaskResult.SUCCESS
