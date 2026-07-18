'农贸作物任务:按所选路线逐行收割 → 播种 → 浇水。'
from __future__ import annotations

from daily import regions as R
from daily.tasks._field import FieldTask


class FarmTask(FieldTask):
    task_id = "farm"
    name = "农贸作物"
    NODE_PT = R.PT_NODE_FARM
    ROUTE_SECOND = "第二列"
    ROUTE_FIFTH = "第五列"
    ROUTES = (ROUTE_SECOND, ROUTE_FIFTH)
    DEFAULT_ROUTE = ROUTE_SECOND
    SECOND_ROUTE_EXIT_MISSES = 5
    FIFTH_ROUTE_EXIT_MISSES = 3
    ACTION_SETTLE_S = 3.0
    ACTION_KIND_WATER_TH = 0.85  
    HANDLE_HIGH_VALUE_WARNING = True

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.route = self.DEFAULT_ROUTE
        from runtime_guard import dev_log
        try:
            from daily.config import DailyConfig
            route = str(DailyConfig().param(self.task_id, "route", self.DEFAULT_ROUTE))
            if route in self.ROUTES:
                self.route = route
            else:
                dev_log(
                    f"[daily] {self.name}: 未知路线 {route!r}，回退默认{self.DEFAULT_ROUTE}"
                )
        except Exception as exc:
            dev_log(f"[daily] {self.name}: 路线配置读取失败，回退默认{self.DEFAULT_ROUTE}", exc)
        
        
        self.FIELD_EXIT_MISSES = (
            self.SECOND_ROUTE_EXIT_MISSES
            if self.route == self.ROUTE_SECOND else self.FIFTH_ROUTE_EXIT_MISSES
        )
        dev_log(f"[daily] {self.name}: 路线={self.route}"
                f" 连续无框结束阈值={self.FIELD_EXIT_MISSES}")

    def _left_to_col1(self) -> None:
        '第五列保留现有 A×3；第二列从落点所在直线直接向前。'
        if self.route == self.ROUTE_FIFTH:
            self.ctx.log(f"{self.name}:选择{self.route}路线 → 执行现有 A×{self.LEFT_TAPS} 横移")
            super()._left_to_col1()
            return
        self._clear_action_evidence()
        self.ctx.log(f"{self.name}:选择{self.route}路线 → 不按 A，沿当前列 W 碎步直行")
