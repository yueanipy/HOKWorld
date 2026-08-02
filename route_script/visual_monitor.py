'后台验证路线小地图视觉锚点。'
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from runtime_guard import dev_log

from .checkpoint_monitor import CheckpointResult
from .frame_stream import RouteFrameStream
from .visual_anchor import (
    VisualAnchor,
    compare_visual_anchor,
    load_visual_anchor,
)


@dataclass(frozen=True)
class _VisualRequest:
    request_id: int
    asset: str
    max_offset: float
    generation: int


class RouteVisualMonitor:
    '只读比较录制与回放的小地图特征，不发送任何输入。'

    TIMEOUT_S = 0.90
    POLL_INTERVAL_S = 0.16
    REQUIRED_MATCHES = 2
    REQUIRED_MISMATCHES = 3
    MAX_PENDING = 4
    MAX_RESULTS = 16

    def __init__(
            self, ctx, *, frames: RouteFrameStream,
            asset_root: Path | str, log=print) -> None:
        self.ctx = ctx
        self.frames = frames
        self.asset_root = Path(asset_root).resolve()
        self.log = log
        self._condition = threading.Condition()
        self._pending: deque[_VisualRequest] = deque()
        self._results: deque[CheckpointResult] = deque(maxlen=self.MAX_RESULTS)
        self._cache: dict[str, VisualAnchor] = {}
        self._active = False
        self._generation = 0
        self._deadline_shift = 0.0
        self._stop = False
        self._thread: threading.Thread | None = None

    def submit_visual(
            self, request_id: int, asset: str, *,
            max_offset: float, retry: int = 2) -> bool:
        '提交视觉检查；retry 由播放器统一执行。'
        del retry
        with self._condition:
            if self._stop or len(self._pending) + int(self._active) >= self.MAX_PENDING:
                return False
            self._pending.append(_VisualRequest(
                int(request_id), str(asset),
                max(1.0, float(max_offset)), self._generation))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="RouteVisualMonitor",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def reset(self) -> None:
        '取消界面切换前的视觉请求。'
        with self._condition:
            self._generation += 1
            self._pending.clear()
            self._results.clear()
            self._condition.notify_all()

    def mark_anchor(self, _route_at: float, _actual_at: float | None = None) -> None:
        self.reset()

    def shift_time(self, seconds: float) -> None:
        '阻塞动作暂停帧流时同步顺延当前检查期限。'
        value = max(0.0, float(seconds))
        if value <= 0.0:
            return
        with self._condition:
            self._deadline_shift += value
            self._condition.notify_all()

    def poll_result(self) -> CheckpointResult | None:
        with self._condition:
            return self._results.popleft() if self._results else None

    def has_pending(self) -> bool:
        with self._condition:
            return bool(self._active or self._pending)

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._generation += 1
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        self.frames.set_checkpoint_active(False)
        if thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive():
                dev_log("[route] 视觉检查线程未在3秒内结束")
        with self._condition:
            self._thread = None
            self._results.clear()
            self._cache.clear()

    def _run(self) -> None:
        while True:
            request: _VisualRequest | None = None
            with self._condition:
                while not self._stop and not self._pending:
                    _notified = self._condition.wait()
                if self._stop:
                    return
                request = self._pending.popleft()
                self._active = True
            self.frames.set_checkpoint_active(True)
            try:
                result = self._check(request)
            except Exception as exc:
                dev_log("[route] 路线视觉检查异常", exc)
                result = CheckpointResult(
                    False, "visual_unavailable", 0,
                    request_id=request.request_id)
            finally:
                self.frames.set_checkpoint_active(False)
            with self._condition:
                self._active = False
                if self._stop:
                    return
                if request.generation == self._generation:
                    self._results.append(result)
                self._condition.notify_all()

    def _check(self, request: _VisualRequest) -> CheckpointResult:
        anchor = self._load(request.asset)
        if anchor is None:
            return CheckpointResult(
                False, "visual_unavailable", 0,
                request_id=request.request_id)
        started = float(self.ctx.logical_time())
        with self._condition:
            shift_base = self._deadline_shift
        deadline = started + self.TIMEOUT_S
        last_sequence = 0
        observations = matches = mismatches = 0
        last_offset: float | None = None
        while not self._cancelled(request):
            now = float(self.ctx.logical_time())
            with self._condition:
                shifted_deadline = deadline + max(
                    0.0, self._deadline_shift - shift_base)
            if now >= shifted_deadline:
                break
            if self.ctx.should_stop():
                return CheckpointResult(
                    False, "stopped", observations,
                    distance=last_offset,
                    elapsed=max(0.0, now - started),
                    timeout=self.TIMEOUT_S,
                    request_id=request.request_id)
            frame = self.frames.wait_next(
                last_sequence,
                min(self.POLL_INTERVAL_S, max(0.01, shifted_deadline - now)),
            )
            if frame is None:
                continue
            last_sequence = frame.sequence
            observations += 1
            result = compare_visual_anchor(
                anchor, frame.image,
                max_center_offset=request.max_offset)
            if not result.available:
                continue
            last_offset = result.center_offset
            if result.matched:
                matches += 1
                mismatches = 0
                if matches >= self.REQUIRED_MATCHES:
                    return CheckpointResult(
                        True, "visual_checkpoint_reached", observations,
                        distance=last_offset,
                        elapsed=max(0.0, float(self.ctx.logical_time()) - started),
                        timeout=self.TIMEOUT_S,
                        request_id=request.request_id)
            else:
                mismatches += 1
                matches = 0
                if mismatches >= self.REQUIRED_MISMATCHES:
                    return CheckpointResult(
                        False, "visual_deviation", observations,
                        distance=last_offset,
                        elapsed=max(0.0, float(self.ctx.logical_time()) - started),
                        timeout=self.TIMEOUT_S,
                        request_id=request.request_id)
        return CheckpointResult(
            False, "visual_unavailable", observations,
            distance=last_offset,
            elapsed=max(0.0, float(self.ctx.logical_time()) - started),
            timeout=self.TIMEOUT_S,
            request_id=request.request_id)

    def _load(self, relative: str) -> VisualAnchor | None:
        name = str(relative).replace("\\", "/").strip("/")
        target = (self.asset_root / name).resolve()
        if target.parent != (self.asset_root / "anchors").resolve():
            return None
        if not target.is_file():
            return None
        cached = self._cache.get(name)
        if cached is None:
            cached = load_visual_anchor(target)
            self._cache[name] = cached
        return cached

    def _cancelled(self, request: _VisualRequest) -> bool:
        with self._condition:
            return bool(self._stop or request.generation != self._generation)


class RouteMonitorHub:
    '合并坐标和视觉检查结果，保持播放器只有一个监控接口。'

    def __init__(self, coordinate=None, visual=None) -> None:
        self.coordinate = coordinate
        self.visual = visual

    def submit(self, *args, **kwargs) -> bool:
        return bool(self.coordinate and self.coordinate.submit(*args, **kwargs))

    def submit_visual(self, *args, **kwargs) -> bool:
        return bool(self.visual and self.visual.submit_visual(*args, **kwargs))

    def reset(self) -> None:
        for monitor in (self.coordinate, self.visual):
            callback = getattr(monitor, "reset", None)
            if callable(callback):
                callback()

    def mark_anchor(self, route_at: float, actual_at: float | None = None) -> None:
        for monitor in (self.coordinate, self.visual):
            callback = getattr(monitor, "mark_anchor", None)
            if callable(callback):
                callback(route_at, actual_at)

    def shift_time(self, seconds: float) -> None:
        for monitor in (self.coordinate, self.visual):
            callback = getattr(monitor, "shift_time", None)
            if callable(callback):
                callback(seconds)

    def poll_result(self) -> CheckpointResult | None:

        for monitor in (self.visual, self.coordinate):
            if monitor is None:
                continue
            callback = getattr(monitor, "poll_result", None)
            result = callback() if callable(callback) else None
            if result is not None:
                return result
        return None

    def has_pending(self) -> bool:
        return any(
            bool(callback())
            for monitor in (self.coordinate, self.visual)
            if callable(callback := getattr(monitor, "has_pending", None)))

    def close(self) -> None:
        for monitor in (self.visual, self.coordinate):
            callback = getattr(monitor, "close", None)
            if callable(callback):
                callback()
