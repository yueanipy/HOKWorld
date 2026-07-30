'保留每日钓鱼原有的定时寻岸方案，供回退和对照测试使用。'
from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from runtime_guard import dev_log


WaterRatios = Callable[
    [np.ndarray], tuple[float, float, float, float]]


class LegacyFishingShoreNavigator:
    '执行旧版固定转向、定时前进和水面比例停止逻辑。'

    STRAFE_S = 0.8
    TURN_PX = 300
    MIN_WALK_S = 5.0
    MAX_WALK_S = 10.0
    MID_RATIO = 0.60
    LOW_RATIO = 0.30
    CENTER_RATIO = 0.62
    FRONT_RATIO = 0.35

    def __init__(self, ctx, water_ratios: WaterRatios) -> None:
        self.ctx = ctx
        self.water_ratios = water_ratios

    def run(self) -> bool:
        '运行旧版寻岸流程。'
        ctx = self.ctx
        if not ctx.walk("d", self.STRAFE_S):
            return False
        ctx.sleep(0.20)
        ctx.log(
            f"每日钓鱼(旧版):D {self.STRAFE_S:.1f}秒完成，"
            f"开始按住 W 并同步右转{self.TURN_PX}px")
        start = ctx.logical_time()
        streak = 0
        camera_turned = False
        while (
                ctx.logical_time() - start < self.MAX_WALK_S
                and not ctx.should_stop()):
            if ctx.paused:
                time.sleep(0.08)
                continue
            if not ctx.foreground():
                return False
            with ctx.hold("w") as held:
                if not held:
                    return False
                if not camera_turned:
                    if not ctx.drag_camera(self.TURN_PX, steps=20):
                        return False
                    camera_turned = True
                while (
                        ctx.logical_time() - start < self.MAX_WALK_S
                        and not ctx.should_stop()):
                    if not ctx.action_ready():
                        break
                    frame = ctx.grab_nowait()
                    if frame is None:
                        time.sleep(0.03)
                        continue
                    mid, low, center, front = self.water_ratios(frame)
                    elapsed = ctx.logical_time() - start
                    at_edge = (
                        elapsed >= self.MIN_WALK_S
                        and mid >= self.MID_RATIO
                        and low >= self.LOW_RATIO
                        and center >= self.CENTER_RATIO
                        and front >= self.FRONT_RATIO
                    )
                    streak = streak + 1 if at_edge else 0
                    dev_log(
                        "[daily fishing legacy] "
                        f"mid={mid:.3f} low={low:.3f} "
                        f"center={center:.3f} front={front:.3f} "
                        f"elapsed={elapsed:.1f}s streak={streak}")
                    if streak >= 2:
                        break
                    time.sleep(0.10)
            if streak >= 2:
                break
            if not ctx.paused and not ctx.action_ready():
                return False
        if streak < 2:
            return False
        ctx.sleep(0.85)
        return True
