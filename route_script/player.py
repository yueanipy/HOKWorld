'按逻辑时间回放路线，并处理失焦、重试和输入释放。'
from __future__ import annotations

import ctypes
import re
import threading
import time
from dataclasses import dataclass
from math import ceil
from typing import Any, Callable

import cv2

from daily.context import DailyContext
from winenv import client_rect_on_screen
from runtime_guard import (
    SafeKeyScheduler,
    dev_log,
    release_known_keys,
    safe_move_mouse_relative,
    safe_scroll,
)
from world_map import WorldMapAtlas, teleport_to

from .checkpoint_monitor import CheckpointResult, RouteCheckpointMonitor
from .frame_stream import RouteFrameStream
from .model import RouteEvent, RouteScript
from .store import RouteStore
from .visual_monitor import RouteMonitorHub, RouteVisualMonitor



Log = Callable[[str], None]
StateCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


_VK_SPECIAL = {
    "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "esc": 0x1B, "space": 0x20,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


@dataclass(frozen=True)
class RouteRunResult:
    '保存路线播放器最终结果。'

    success: bool
    reason: str
    completed_events: int
    skipped_checkpoints: int = 0


@dataclass(frozen=True)
class _ActiveMouseHold:
    '保存可在暂停后恢复的鼠标按住状态。'

    deadline: float
    positioned: bool
    x: float
    y: float


@dataclass(frozen=True)
class _ReplayAnchor:
    '保存可通过公共传送重新建立的路线重试起点。'

    target: str
    route_at: float
    next_index: int


class RoutePlayer:
    '执行一条不可变路线；UI只通过回调获取低频状态。'

    def __init__(
            self, route: RouteScript, *, loop_count: int = 1,
            coordinate_correction: bool | None = None,
            log: Log = print, on_state: StateCallback | None = None,
            on_progress: ProgressCallback | None = None,
            store: RouteStore | None = None) -> None:
        route.validate()
        self.route = route
        self.loop_count = max(1, min(999, int(loop_count)))
        self.coordinate_correction = (
            route.metadata.coordinate_correction
            if coordinate_correction is None else bool(coordinate_correction))
        self.log = log
        self.on_state = on_state or (lambda _text: None)
        self.on_progress = on_progress or (lambda _current, _total: None)
        self.store = store or RouteStore()
        self._stop = threading.Event()
        self._manual_pause = threading.Event()
        self._ctx: DailyContext | None = None
        self._scheduler: SafeKeyScheduler | None = None
        self._active_holds: dict[str, float] = {}
        self._active_mouse_holds: dict[str, _ActiveMouseHold] = {}
        self._timeline_start = 0.0
        self._was_suspended = False
        self._relative_mouse_scale = 1.0
        self._atlas: WorldMapAtlas | None = None
        self._frame_stream: RouteFrameStream | None = None
        self._checkpoint_failure: CheckpointResult | None = None

    def stop(self) -> None:
        self._stop.set()
        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.release_all()
        ctx = self._ctx
        if ctx is not None:
            ctx.stop("route_stop")
        release_known_keys(self.log)

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._manual_pause.set()

            self._was_suspended = True
            if self._scheduler is not None:
                self._scheduler.release_all()
            if self._ctx is not None:
                self._ctx.set_paused(True)
            release_known_keys(self.log)
        else:
            self._manual_pause.clear()
            if self._ctx is not None:
                self._ctx.set_paused(False)

    def run(self) -> RouteRunResult:
        ctx = DailyContext(log=self.log)
        self._ctx = ctx
        completed = 0
        skipped_coordinates = 0
        navigator: RouteMonitorHub | None = None
        frame_stream: RouteFrameStream | None = None
        diagnostics_enabled = False
        try:
            if self._stop.is_set():
                return RouteRunResult(False, "stopped", 0)
            if not ctx.start():
                return RouteRunResult(False, "game_not_found", 0)
            needs_checkpoint_monitor = bool(
                self.coordinate_correction
                and any(event.action == "checkpoint"
                        for event in self.route.events))
            needs_visual_monitor = bool(
                self.coordinate_correction
                and any(event.action == "visual_checkpoint"
                        for event in self.route.events))
            if (needs_checkpoint_monitor or needs_visual_monitor
                    or diagnostics_enabled):
                frame_stream = RouteFrameStream(
                    ctx, diagnostics=diagnostics_enabled)
                frame_stream.start()
                self._frame_stream = frame_stream
            if self._stop.is_set() or ctx.should_stop():
                return RouteRunResult(False, "stopped", 0)
            self._warn_environment(ctx)
            self._relative_mouse_scale = self._mouse_dpi_scale(ctx)
            needs_atlas = bool(
                self.route.metadata.start_teleport
                or needs_checkpoint_monitor
                or any(event.action == "teleport"
                       for event in self.route.events))
            atlas = WorldMapAtlas() if needs_atlas else None
            self._atlas = atlas

            coordinate_monitor = (
                RouteCheckpointMonitor(
                    ctx, atlas=atlas, frames=frame_stream, log=self.log)
                if (needs_checkpoint_monitor and atlas is not None
                    and frame_stream is not None) else None)
            visual_monitor = (
                RouteVisualMonitor(
                    ctx,
                    frames=frame_stream,
                    asset_root=(
                        self.store.directory / "snapshots"
                        / RouteStore.normalize_name(self.route.metadata.name)),
                    log=self.log,
                )
                if (needs_visual_monitor and frame_stream is not None)
                else None)
            navigator = (
                RouteMonitorHub(coordinate_monitor, visual_monitor)
                if coordinate_monitor is not None or visual_monitor is not None
                else None)
            self._scheduler = SafeKeyScheduler(
                stop_check=lambda: (
                    self._stop.is_set() or self._manual_pause.is_set()
                    or ctx.should_stop()),
                foreground_check=ctx.foreground,
                log=self.log,
            )

            total = len(self.route.events) * self.loop_count
            for loop_index in range(self.loop_count):
                if self._stop.is_set() or ctx.should_stop():
                    return RouteRunResult(
                        False, "stopped", completed,
                        skipped_coordinates)
                anchor: _ReplayAnchor | None = None
                if self.route.metadata.start_teleport:
                    self.on_state(f"传送到 {self.route.metadata.start_teleport}")
                    self._pause_route_frames()
                    try:
                        arrived = teleport_to(
                            ctx, self.route.metadata.start_teleport,
                            timeout=30.0, atlas=atlas)
                    finally:
                        self._resume_route_frames()
                    if not arrived:
                        self.log("路线起始传送失败，已停止整条路线")
                        return RouteRunResult(
                            False, "teleport_failed", completed,
                            skipped_coordinates)
                    anchor = _ReplayAnchor(
                        self.route.metadata.start_teleport, 0.0, 0)
                self._timeline_start = ctx.logical_time()
                self._active_holds.clear()
                self._active_mouse_holds.clear()
                if navigator is not None:
                    navigator.mark_anchor(0.0, self._timeline_start)
                retry_counts: dict[int, int] = {}
                event_index = 0
                furthest_event = 0
                while True:
                    event: RouteEvent | None = None
                    if event_index >= len(self.route.events):
                        status = self._wait_checkpoint_completion(
                            ctx, navigator)
                        if status is None:
                            break
                        failure = self._take_checkpoint_failure()
                        failed_event_index = (
                            max(0, len(self.route.events) - 1)
                            if failure is None or failure.request_id is None
                            else int(failure.request_id))
                    else:
                        event = self.route.events[event_index]
                    if (event is not None
                            and not self._wait_event(ctx, event.at, navigator)):
                        failure = self._take_checkpoint_failure()
                        if failure is None:
                            reason = self._runtime_reason(ctx)
                            return RouteRunResult(
                                False, reason, completed,
                                skipped_coordinates)
                        status = str(failure.reason)
                        failed_event_index = (
                            event_index if failure.request_id is None
                            else int(failure.request_id))
                    elif event is not None:
                        status = self._dispatch(
                            event, ctx, navigator, event_index=event_index)
                        failed_event_index = event_index
                        failure = (
                            self._checkpoint_failure
                            or self._poll_checkpoint_monitor(navigator))
                        if failure is not None:
                            status = str(failure.reason)
                            failed_event_index = (
                                event_index if failure.request_id is None
                                else int(failure.request_id))
                    if self._stop.is_set() or ctx.should_stop():
                        return RouteRunResult(
                            False, self._runtime_reason(ctx), completed,
                            skipped_coordinates)
                    if status in {
                            "checkpoint_timeout", "route_deviation",
                            "visual_deviation"}:
                        failure = self._take_checkpoint_failure() or failure
                        used = retry_counts.get(failed_event_index, 0)
                        retry_limit = self._checkpoint_retry_limit(
                            failed_event_index)
                        if anchor is None or used >= retry_limit:
                            self.log(
                                "路线检查失败且没有可用重试次数："
                                f"{status}")
                            return RouteRunResult(
                                False, status, completed,
                                skipped_coordinates)
                        retry_counts[failed_event_index] = used + 1
                        self.on_state(
                            f"检查点失败，从最近传送点重试 "
                            f"{used + 1}/{retry_limit}")
                        if not self._restart_from_anchor(
                                ctx, navigator, anchor):
                            return RouteRunResult(
                                False, "teleport_failed", completed,
                                skipped_coordinates)
                        event_index = anchor.next_index
                        continue
                    if event is None:

                        return RouteRunResult(
                            False, str(status), completed,
                            skipped_coordinates)
                    if status == "teleport_failed":
                        return RouteRunResult(
                            False, "teleport_failed", completed,
                            skipped_coordinates)
                    elif status == "fatal":
                        return RouteRunResult(
                            False, "input_failed", completed,
                            skipped_coordinates)
                    if event.action == "teleport":
                        anchor = _ReplayAnchor(
                            str(event.args["target"]).strip(),
                            float(event.at), event_index + 1)
                    furthest_event = max(furthest_event, event_index + 1)
                    completed = max(
                        completed,
                        loop_index * len(self.route.events) + furthest_event)
                    self.on_progress(completed, total)
                    self._cleanup_holds(ctx.logical_time())
                    event_index += 1
                if not self._wait_active_holds(ctx, navigator):
                    return RouteRunResult(
                        False, self._runtime_reason(ctx), completed,
                        skipped_coordinates)
                if loop_index + 1 < self.loop_count:
                    self.log(f"路线第 {loop_index + 1}/{self.loop_count} 轮完成")

            self.on_state("路线完成")
            return RouteRunResult(
                True, "completed", completed,
                skipped_coordinates)
        except Exception as exc:
            dev_log("[route] 路线播放器异常", exc)
            self.log(f"[错误] {type(exc).__name__}: {exc}")
            return RouteRunResult(
                False, "exception", completed,
                skipped_coordinates)
        finally:
            if navigator is not None:
                try:
                    navigator.close()
                except Exception as exc:
                    dev_log("[route] 检查点监控器停止失败", exc)
            scheduler = self._scheduler
            self._scheduler = None
            if scheduler is not None:
                scheduler.close()
            self._active_holds.clear()
            self._active_mouse_holds.clear()
            self._atlas = None
            if frame_stream is not None:
                try:
                    frame_stream.close()
                except Exception as exc:
                    dev_log("[route] 后台路线截图停止失败", exc)
            self._frame_stream = None
            ctx.close()
            self._ctx = None
            release_known_keys(self.log)

    def _wait_event(
            self, ctx: DailyContext, event_at: float,
            navigator: Any | None) -> bool:
        target = self._timeline_start + max(0.0, float(event_at))
        while ctx.logical_time() < target:
            if self._stop.is_set() or ctx.should_stop():
                return False
            if self._poll_checkpoint_monitor(navigator) is not None:
                return False
            if not self._runtime_ready(ctx, navigator):
                return False
            self._cleanup_holds(ctx.logical_time())
            time.sleep(min(0.02, max(0.001, target - ctx.logical_time())))
        if self._poll_checkpoint_monitor(navigator) is not None:
            return False
        return self._runtime_ready(ctx, navigator)

    def _poll_checkpoint_monitor(
            self, navigator: Any | None) -> CheckpointResult | None:
        '读取后台结果；只记录状态，不在回放热路径中执行识别。'
        if navigator is None:
            return None
        poll = getattr(navigator, "poll_result", None)
        if not callable(poll):
            return None
        while True:
            result = poll()
            if result is None:
                return None
            if bool(result.success):
                if str(result.reason) == "visual_checkpoint_reached":
                    self.on_state(
                        f"视觉检查已确认，偏移 {float(result.distance or 0.0):.1f}px")
                else:
                    self.on_state(
                        f"检查点已确认，偏差 {float(result.distance or 0.0):.0f}")
                continue
            if str(result.reason) in {
                    "coordinate_unavailable", "visual_unavailable"}:

                self.log(f"路线检查暂不可用：{result.reason}，继续原路线")
                continue
            if str(result.reason) == "stopped":
                if self._stop.is_set() or (self._ctx and self._ctx.should_stop()):
                    return None
            self.log(
                "路线检查点失败："
                f"{result.reason}，偏差 {float(result.distance):.0f}"
                if result.distance is not None else
                f"路线检查点失败：{result.reason}，坐标不可用")
            self._checkpoint_failure = result
            return result

    def _checkpoint_retry_limit(self, event_index: int) -> int:
        '读取触发失败的检查点自身重试上限。'
        if not 0 <= int(event_index) < len(self.route.events):
            return 2
        event = self.route.events[int(event_index)]
        if event.action not in {"checkpoint", "visual_checkpoint"}:
            return 2
        try:
            return max(0, min(5, int(event.args.get("retry", 2))))
        except (TypeError, ValueError):
            return 2

    def _take_checkpoint_failure(self) -> CheckpointResult | None:
        '取走一次后台检查失败，避免重复触发重试。'
        result = self._checkpoint_failure
        self._checkpoint_failure = None
        return result

    def _wait_checkpoint_completion(
            self, ctx: DailyContext,
            navigator: Any | None) -> str | None:
        '路线事件发送完后等待最后检查收尾，不影响已完成的键鼠时间轴。'
        if navigator is None:
            return None
        pending = getattr(navigator, "has_pending", None)
        if not callable(pending):
            return None
        while pending():
            failure = self._poll_checkpoint_monitor(navigator)
            if failure is not None:
                return str(failure.reason)
            if self._stop.is_set() or ctx.should_stop():
                return self._runtime_reason(ctx)
            if not self._runtime_ready(ctx, navigator):
                return self._runtime_reason(ctx)
            time.sleep(0.02)
        failure = self._poll_checkpoint_monitor(navigator)
        return None if failure is None else str(failure.reason)

    def _runtime_ready(
            self, ctx: DailyContext, navigator: Any | None) -> bool:
        if self._manual_pause.is_set():
            if not self._was_suspended:
                self._suspend_inputs()
                self._was_suspended = True
                self.on_state("已暂停")
            while self._manual_pause.is_set() and not self._stop.wait(0.08):
                if ctx.should_stop():
                    return False
            if self._stop.is_set() or ctx.should_stop():
                return False
            ctx.set_paused(False)
            if navigator is not None:
                navigator.reset()
            self._resume_holds(ctx.logical_time())
            self._was_suspended = False
            self.on_state("运行中")

        if ctx.foreground():
            if self._was_suspended:
                if navigator is not None:
                    navigator.reset()
                self._resume_holds(ctx.logical_time())
                self._was_suspended = False
                self.on_state("游戏已回到前台，继续路线")
            return True

        if not self._was_suspended:
            self._suspend_inputs()
            self._was_suspended = True
            self.on_state("游戏不在前台，路线已暂停")
        if not ctx.wait_foreground(timeout=None):
            return False
        if navigator is not None:
            navigator.reset()
        self._resume_holds(ctx.logical_time())
        self._was_suspended = False
        self.on_state("游戏已回到前台，继续路线")
        return True

    def _dispatch(
            self, event: RouteEvent, ctx: DailyContext,
            navigator: Any | None,
            *, event_index: int | None = None) -> str:
        action = event.action
        args = event.args
        if action == "wait":
            return "ok"
        if action == "key_hold":
            key = str(args["key"]).lower()
            vk = _vk_for_key(key)
            duration = float(args["duration"])
            deadline = self._timeline_start + float(event.at) + duration

            def press_key() -> bool:
                remaining = deadline - ctx.logical_time()
                if remaining <= 0.0:
                    return True
                return bool(
                    self._scheduler
                    and self._scheduler.press_mapped_key(vk, remaining))

            if self._scheduler is None or not self._input_with_focus_retry(
                    ctx, navigator, press_key):
                return "fatal"
            if deadline > ctx.logical_time():
                self._active_holds[key] = deadline
            return "ok"
        if action == "mouse_click":
            button = str(args["button"]).lower()
            duration = float(args["duration"])
            positioned = bool(args.get("positioned", button == "left"))
            x, y = float(args["x"]), float(args["y"])
            deadline = self._timeline_start + float(event.at) + duration

            def press_mouse() -> bool:
                remaining = deadline - ctx.logical_time()
                if remaining <= 0.0:
                    return True
                position = (
                    self._cursor_position(ctx, x, y)
                    if positioned else None)
                if positioned and position is None:
                    return False
                return bool(
                    self._scheduler
                    and self._scheduler.press_mouse(
                        button, remaining, cursor_position=position))

            ok = bool(
                self._scheduler
                and self._input_with_focus_retry(
                    ctx, navigator, press_mouse))
            if ok and deadline > ctx.logical_time():
                self._active_mouse_holds[button] = _ActiveMouseHold(
                    deadline, positioned, x, y)
            return "ok" if ok else "fatal"
        if action == "mouse_drag":
            ok = self._input_with_focus_retry(
                ctx, navigator,
                lambda: ctx.drag(
                    (float(args["x1"]), float(args["y1"])),
                    (float(args["x2"]), float(args["y2"])),
                    duration_s=float(args["duration"]),
                ),
            )
            return "ok" if ok else "fatal"
        if action == "mouse_move_rel":
            ok = self._move_relative_resumable(
                ctx, navigator,
                int(args["dx"]), int(args["dy"]),
                float(args.get("duration", 0.0)),
            )
            return "ok" if ok else "fatal"
        if action == "heading_anchor":

            return "ok"
        if action == "mouse_wheel":
            positioned = bool(args.get("positioned", True))
            ok = self._input_with_focus_retry(
                ctx, navigator,
                lambda: (
                    ctx.scroll(
                        int(args["notches"]),
                        (float(args["x"]), float(args["y"])))
                    if positioned else
                    safe_scroll(
                        int(args["notches"]),
                        stop_check=lambda: (
                            self._stop.is_set()
                            or self._manual_pause.is_set()
                            or ctx.should_stop()),
                        foreground_check=ctx.foreground,
                        log=self.log,
                    )
                ),
            )
            return "ok" if ok else "fatal"
        if action == "checkpoint":
            if not self.coordinate_correction:
                return "ok"
            if navigator is None:
                self.log("路线检查点监测器不可用")
                return "coordinate_unavailable"
            submitted = navigator.submit(
                int(event_index if event_index is not None else -1),
                (float(args["x"]), float(args["y"])),
                route_at=float(event.at),
                tolerance=float(args["tolerance"]),
                retry=int(args.get("retry", 2)),
            )
            if not submitted:
                self.log("路线检查点队列已满或已停止")
                return "ok"
            return "ok"
        if action == "visual_checkpoint":
            if not self.coordinate_correction:
                return "ok"
            if navigator is None:
                self.log("路线视觉检查器不可用，继续原路线")
                return "ok"
            submitted = navigator.submit_visual(
                int(event_index if event_index is not None else -1),
                str(args["asset"]),
                max_offset=float(args.get("max_offset", 14.0)),
                retry=int(args.get("retry", 2)),
            )
            if not submitted:
                self.log("路线视觉检查队列已满，继续原路线")
            return "ok"
        if action == "teleport":
            target = str(args["target"]).strip()
            teleport_started = ctx.logical_time()
            self._suspend_inputs()
            self.on_state(f"图谱传送到 {target}")
            if self._atlas is None:
                self.log("图谱传送资源未初始化，路线已停止")
                return "teleport_failed"
            self._pause_route_frames()
            try:
                ok = teleport_to(
                    ctx, target, timeout=30.0, atlas=self._atlas)


                self._finish_blocking_action(
                    ctx, teleport_started, navigator=navigator)
                if ok and navigator is not None:
                    navigator.mark_anchor(
                        float(event.at), ctx.logical_time())
            finally:
                self._resume_route_frames()
            if not ok:
                self.log(f"图谱传送失败：{target}，路线已停止")
                return "teleport_failed"
            self.on_state(f"已到达 {target}")
            return "ok"
        if action == "snapshot":
            self._save_snapshot(ctx, str(args["name"]))
            return "ok"
        return "fatal"

    def _runtime_reason(self, ctx: DailyContext) -> str:
        if self._stop.is_set():
            return "stopped"
        return "game_closed" if ctx.should_stop() else "runtime_not_ready"

    def _input_with_focus_retry(
            self, ctx: DailyContext, navigator: Any | None,
            operation: Callable[[], bool]) -> bool:
        '输入仅因临时失焦失败时，等待HUD恢复后重做一次。'
        if operation():
            return True
        if self._stop.is_set() or ctx.should_stop():
            return False
        if ctx.foreground() and not self._manual_pause.is_set():
            return False
        if not self._runtime_ready(ctx, navigator):
            return False
        return bool(operation())

    def _move_relative_resumable(
            self, ctx: DailyContext, navigator: Any | None,
            dx: int, dy: int, duration: float) -> bool:
        '分段转镜头，失焦只重做尚未发送的小段。'
        total_x = int(round(int(dx) * self._relative_mouse_scale))
        total_y = int(round(int(dy) * self._relative_mouse_scale))
        seconds = max(0.0, min(5.0, float(duration)))


        steps = 1 if seconds <= 0.025 else max(
            1, min(120, max(
                int(ceil(seconds / 0.01)),
                int(ceil(max(abs(total_x), abs(total_y)) / 40.0)),
            )))
        moved_x = moved_y = 0
        for index in range(1, steps + 1):
            if self._poll_checkpoint_monitor(navigator) is not None:
                return False
            if self._was_suspended or self._manual_pause.is_set():
                if not self._runtime_ready(ctx, navigator):
                    return False
            target_x = int(round(total_x * index / steps))
            target_y = int(round(total_y * index / steps))
            step_x, step_y = target_x - moved_x, target_y - moved_y
            if step_x or step_y:
                ok = self._input_with_focus_retry(
                    ctx, navigator,
                    lambda sx=step_x, sy=step_y: safe_move_mouse_relative(
                        sx, sy, duration_s=0.0,
                        stop_check=lambda: (
                            self._stop.is_set() or self._manual_pause.is_set()
                            or ctx.should_stop()),
                        foreground_check=ctx.foreground,
                        log=self.log,
                    ),
                )
                if not ok:
                    return False
            moved_x, moved_y = target_x, target_y
            if seconds > 0.0 and index < steps:
                ctx.sleep(seconds / steps)
        return True

    def _suspend_inputs(self) -> None:
        if self._scheduler is not None:
            self._scheduler.release_all()
        release_known_keys(self.log)

    def _pause_route_frames(self) -> None:
        '在阻塞视觉闭环前让后台单一截图生产者安全让位。'
        stream = self._frame_stream
        if stream is not None:
            stream.pause()

    def _resume_route_frames(self) -> None:
        '阻塞视觉闭环结束后恢复后台低频截图。'
        stream = self._frame_stream
        if stream is not None:
            stream.resume()

    def _finish_blocking_action(
            self, ctx: DailyContext, logical_started: float,
            *, navigator: Any | None = None) -> None:
        '把视觉闭环耗时从路线时间轴中扣除，并安全恢复未到期按键。'
        extra = max(0.0, ctx.logical_time() - float(logical_started))
        self._timeline_start += extra
        if navigator is not None:
            shift_time = getattr(navigator, "shift_time", None)
            if callable(shift_time):
                shift_time(extra)
        if extra > 0.0:
            self._active_holds = {
                key: deadline + extra
                for key, deadline in self._active_holds.items()
            }
            self._active_mouse_holds = {
                button: _ActiveMouseHold(
                    hold.deadline + extra,
                    hold.positioned,
                    hold.x,
                    hold.y,
                )
                for button, hold in self._active_mouse_holds.items()
            }
        if (not self._stop.is_set() and not ctx.should_stop()
                and not self._manual_pause.is_set()):
            self._resume_holds(ctx.logical_time())


            self._was_suspended = False

    def _restart_from_anchor(
            self, ctx: DailyContext,
            navigator: Any | None,
            anchor: _ReplayAnchor) -> bool:
        '重新传送到最近硬检查点，并从该段时间轴继续。'
        self._suspend_inputs()
        self._active_holds.clear()
        self._active_mouse_holds.clear()
        if self._atlas is None or self._stop.is_set() or ctx.should_stop():
            return False
        self.log(f"重新传送到路线检查点：{anchor.target}")
        self._pause_route_frames()
        arrived = False
        try:
            arrived = teleport_to(
                ctx, anchor.target, timeout=30.0, atlas=self._atlas)
            if arrived:
                now = ctx.logical_time()
                self._timeline_start = now - max(0.0, float(anchor.route_at))
                if navigator is not None:
                    navigator.mark_anchor(anchor.route_at, now)
        finally:
            self._resume_route_frames()
        if not arrived:
            self.log(f"路线检查点传送失败：{anchor.target}")
            return False
        self._was_suspended = False
        return True

    def _resume_holds(self, logical_now: float) -> None:
        scheduler = self._scheduler
        if scheduler is None:
            return
        for key, deadline in tuple(self._active_holds.items()):
            remaining = deadline - logical_now
            if remaining <= 0.0:
                self._active_holds.pop(key, None)
                continue
            scheduler.press_mapped_key(_vk_for_key(key), remaining)
        ctx = self._ctx
        for button, hold in tuple(self._active_mouse_holds.items()):
            remaining = hold.deadline - logical_now
            if remaining <= 0.0:
                self._active_mouse_holds.pop(button, None)
                continue
            position = None
            if hold.positioned:
                if ctx is None:
                    self._active_mouse_holds.pop(button, None)
                    continue
                position = self._cursor_position(ctx, hold.x, hold.y)
                if position is None:
                    self._active_mouse_holds.pop(button, None)
                    continue
            scheduler.press_mouse(
                button, remaining, cursor_position=position)

    def _cleanup_holds(self, logical_now: float) -> None:
        for key, deadline in tuple(self._active_holds.items()):
            if deadline <= logical_now:
                self._active_holds.pop(key, None)
        for button, hold in tuple(self._active_mouse_holds.items()):
            if hold.deadline <= logical_now:
                self._active_mouse_holds.pop(button, None)

    def _wait_active_holds(
            self, ctx: DailyContext, navigator: Any | None) -> bool:
        while ((self._active_holds or self._active_mouse_holds)
               and not self._stop.is_set() and not ctx.should_stop()):
            if not self._runtime_ready(ctx, navigator):
                return False
            self._cleanup_holds(ctx.logical_time())
            time.sleep(0.02)
        return not self._stop.is_set() and not ctx.should_stop()

    def _active_hold_names(self) -> list[str]:
        return [
            *sorted(self._active_holds),
            *(f"mouse_{button}"
              for button in sorted(self._active_mouse_holds)),
        ]

    @staticmethod
    def _cursor_position(
            ctx: DailyContext, x: float, y: float) -> tuple[int, int] | None:
        hwnd = int(ctx.hwnd or 0)
        if not hwnd:
            return None
        try:
            left, top, width, height = client_rect_on_screen(hwnd)
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        return (
            int(left + max(0.0, min(1.0, x)) * width),
            int(top + max(0.0, min(1.0, y)) * height),
        )


    def _warn_environment(self, ctx: DailyContext) -> None:
        metadata = self.route.metadata
        has_map_observations = any(
            event.action in {
                "checkpoint", "visual_checkpoint", "heading_anchor"}
            for event in self.route.events)
        if has_map_observations:
            if self.coordinate_correction and any(
                    event.action in {"checkpoint", "visual_checkpoint"}
                    for event in self.route.events):
                self.log(
                    "[提示] 小地图坐标和视觉检查仅判断路线偏差，"
                    "不会据此转向或移动")
            else:
                self.log(
                    "[提示] 路线中的地图节点和方向锚点仅用于记录，"
                    "本次回放不会据此转向或移动")
        if not ctx.hwnd or not metadata.client_width or not metadata.client_height:
            return
        try:
            _x, _y, width, height = client_rect_on_screen(ctx.hwnd)
        except Exception:
            return
        if (width, height) != (metadata.client_width, metadata.client_height):
            self.log(
                "[提示] 当前游戏客户区与录制时不同："
                f"当前 {width}x{height}，录制 {metadata.client_width}x{metadata.client_height}；"
                "点击坐标会自适应，固定时长和镜头移动可能存在偏差")

    def _mouse_dpi_scale(self, ctx: DailyContext) -> float:
        '按录制和回放窗口DPI比例换算相对鼠标增量。'
        recorded = max(0.5, min(4.0, float(self.route.metadata.dpi_scale or 1.0)))
        current = recorded
        try:
            current = float(ctypes.windll.user32.GetDpiForWindow(
                int(ctx.hwnd or 0))) / 96.0
        except Exception:
            pass
        current = max(0.5, min(4.0, current))
        scale = current / recorded
        if abs(scale - 1.0) >= 0.01:
            self.log(
                f"路线镜头已按DPI换算：录制 {recorded:.2f}，"
                f"当前 {current:.2f}，倍率 {scale:.3f}")
        return scale

    def _save_snapshot(self, ctx: DailyContext, name: str) -> None:
        try:
            route_name = RouteStore.normalize_name(self.route.metadata.name)
            snapshot_root = (self.store.directory / "snapshots").resolve()
            directory = (snapshot_root / route_name).resolve()
            if directory.parent != snapshot_root:
                raise ValueError("截图目录越界")
        except Exception as exc:
            dev_log("[route] 路线截图目录无效", exc)
            return
        self._pause_route_frames()
        try:
            frame = ctx.grab()
        finally:
            self._resume_route_frames()
        if frame is None:
            return
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "snapshot"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{int(time.time() * 1000)}_{safe_name}.jpg"
        try:
            ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                data.tofile(str(path))
        except Exception as exc:
            dev_log(f"[route] 路线截图保存失败: {path}", exc)


def _vk_for_key(key: str) -> int:
    value = str(key).lower()
    if len(value) == 1 and "a" <= value <= "z":
        return ord(value.upper())
    if len(value) == 1 and "0" <= value <= "9":
        return ord(value)
    if value.startswith("f") and value[1:].isdigit():
        number = int(value[1:])
        if 1 <= number <= 12:
            return 0x70 + number - 1
    try:
        return _VK_SPECIAL[value]
    except KeyError as exc:
        raise ValueError(f"不支持的路线按键 {key!r}") from exc

