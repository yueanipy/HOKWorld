'后台只读验证路线检查点，不阻塞键鼠时间轴。'
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from math import hypot

from runtime_guard import dev_log
from world_map.core import WorldMapAtlas
from world_map.minimap import MiniMapPoseRecognizer, MiniMapPoseTracker

from .frame_stream import RouteFrameStream


@dataclass(frozen=True)
class CheckpointResult:
    '保存一次路线检查点验证结果。'

    success: bool
    reason: str
    observations: int
    source: tuple[float, float] | None = None
    distance: float | None = None
    elapsed: float = 0.0
    timeout: float = 0.0
    request_id: int | None = None


@dataclass(frozen=True)
class _CheckpointRequest:
    request_id: int
    target: tuple[float, float]
    route_at: float
    tolerance: float
    retry: int
    generation: int


class RouteCheckpointMonitor:
    '后台判断路线偏航，不修改角色、镜头或回放时间轴。'

    TIMEOUT_MULTIPLIER = 1.5
    MIN_TIMEOUT_S = 0.9

    MIN_DISTANCE_TOLERANCE = 900.0
    DISTANCE_MULTIPLIER = 1.5
    POLL_INTERVAL_S = 0.16
    DEADLINE_EPSILON_S = 0.12
    MAX_PENDING = 8
    MAX_RESULTS = 16

    def __init__(
            self, ctx, *, atlas: WorldMapAtlas,
            frames: RouteFrameStream, log=print) -> None:
        self.ctx = ctx
        self.log = log
        self.frames = frames
        self.recognizer = MiniMapPoseRecognizer(atlas)
        self.tracker = MiniMapPoseTracker(
            window=5, required=2, max_atlas_jump=85.0)
        now = float(ctx.logical_time())
        self._route_anchor_at = 0.0
        self._actual_anchor_at = now
        self._last_expected: tuple[float, float] | None = None
        self._last_source: tuple[float, float] | None = None
        self._condition = threading.Condition()
        self._pending: deque[_CheckpointRequest] = deque()
        self._results: deque[CheckpointResult] = deque(maxlen=self.MAX_RESULTS)
        self._active = False
        self._generation = 0
        self._reset_requested = False
        self._stop = False
        self._thread: threading.Thread | None = None

    def reset(self) -> None:
        '失焦或界面切换后由后台线程清除连续帧缓存。'
        with self._condition:
            self._reset_requested = True
            self._condition.notify_all()

    def mark_anchor(self, route_at: float, actual_at: float | None = None) -> None:
        '传送到HUD后取消旧检查并重置分段计时。'
        with self._condition:
            self._generation += 1
            self._pending.clear()
            self._results.clear()
            self._route_anchor_at = max(0.0, float(route_at))
            self._actual_anchor_at = (
                float(self.ctx.logical_time())
                if actual_at is None else float(actual_at))
            self._last_expected = None
            self._last_source = None
            self._reset_requested = True
            self._condition.notify_all()

    def shift_time(self, seconds: float) -> None:
        '视觉闭环冻结原路线时间轴时同步顺延检查点期限。'
        value = max(0.0, float(seconds))
        if value <= 0.0:
            return
        with self._condition:
            self._actual_anchor_at += value
            self._condition.notify_all()

    def submit(
            self, request_id: int, target: tuple[float, float], *,
            route_at: float, tolerance: float, retry: int = 2) -> bool:
        '快速提交检查请求；真正截图与定位在后台完成。'
        with self._condition:
            queued = len(self._pending) + int(self._active)
            if self._stop or queued >= self.MAX_PENDING:
                return False
            request = _CheckpointRequest(
                int(request_id),
                (float(target[0]), float(target[1])),
                max(0.0, float(route_at)),
                max(0.0, float(tolerance)),
                max(0, int(retry)),
                self._generation,
            )
            self._pending.append(request)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="RouteCheckpointMonitor",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def poll_result(self) -> CheckpointResult | None:
        '立即读取一个已完成结果，不等待后台识别。'
        with self._condition:
            return self._results.popleft() if self._results else None

    def has_pending(self) -> bool:
        '返回是否仍有后台检查尚未完成。'
        with self._condition:
            return bool(self._active or self._pending)

    def close(self) -> None:
        '取消请求并有界等待识别线程退出。'
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
                dev_log("[route] 检查点识别线程未在3秒内结束")
        with self._condition:
            self._thread = None
            self._results.clear()

    def _run(self) -> None:
        while True:
            request: _CheckpointRequest | None = None
            with self._condition:
                while True:
                    if self._stop or self._pending:
                        break
                    self._condition.wait()
                if self._stop:
                    return
                request = self._pending.popleft()
                self._active = True
                if self._reset_requested:
                    self.tracker.reset()
                    self._reset_requested = False
            self.frames.set_checkpoint_active(True)
            try:
                result = self._check_request(request)
            except Exception as exc:
                dev_log("[route] 路线检查点后台识别异常", exc)
                result = CheckpointResult(
                    False, "coordinate_unavailable", 0,
                    request_id=request.request_id)
            finally:
                self.frames.set_checkpoint_active(False)
            with self._condition:
                self._active = False
                if self._stop:
                    return
                if request.generation == self._generation:
                    if result.success:
                        self._route_anchor_at = request.route_at
                        self._actual_anchor_at = float(self.ctx.logical_time())
                        self._last_expected = request.target
                        self._last_source = result.source
                    self._results.append(result)
                self._condition.notify_all()

    def _check_request(self, request: _CheckpointRequest) -> CheckpointResult:
        with self._condition:
            route_anchor_at = self._route_anchor_at
            actual_anchor_at = self._actual_anchor_at
        route_at = max(route_anchor_at, request.route_at)
        expected = max(0.0, route_at - route_anchor_at)
        timeout = max(self.MIN_TIMEOUT_S, expected * self.TIMEOUT_MULTIPLIER)
        deadline = actual_anchor_at + timeout
        allowed_distance = max(
            self.MIN_DISTANCE_TOLERANCE,
            request.tolerance * self.DISTANCE_MULTIPLIER,
        )
        observations = 0
        last_sequence = 0
        last_source: tuple[float, float] | None = None
        last_distance: float | None = None

        while not self._cancelled(request):
            now = float(self.ctx.logical_time())
            elapsed = max(0.0, now - actual_anchor_at)
            if now > deadline + self.DEADLINE_EPSILON_S:
                return self._failure(
                    request, observations, last_source, last_distance,
                    elapsed, timeout, allowed_distance)
            if self.ctx.should_stop():
                return CheckpointResult(
                    False, "stopped", observations,
                    last_source, last_distance, elapsed, timeout,
                    request.request_id)

            frame = self.frames.wait_next(
                last_sequence,
                min(self.POLL_INTERVAL_S, max(0.01, deadline - now)),
            )
            if frame is None:
                continue
            last_sequence = frame.sequence
            observations += 1
            pose = None
            try:
                with self._condition:
                    if self._reset_requested:
                        self.tracker.reset()
                        self._reset_requested = False
                pose = self.tracker.update(self.recognizer.detect(frame.image))
            except Exception as exc:
                dev_log("[route] 路线检查点坐标识别异常", exc)
            if pose is None or pose.source is None:
                continue
            last_source = float(pose.source[0]), float(pose.source[1])
            last_distance = _distance(last_source, request.target)
            now = float(self.ctx.logical_time())
            elapsed = max(0.0, now - actual_anchor_at)
            if last_distance <= allowed_distance:
                if now > deadline + self.DEADLINE_EPSILON_S:
                    return CheckpointResult(
                        False, "checkpoint_timeout", observations,
                        last_source, last_distance, elapsed, timeout,
                        request.request_id)
                return CheckpointResult(
                    True, "checkpoint_reached", observations,
                    last_source, last_distance, elapsed, timeout,
                    request.request_id)

        return CheckpointResult(
            False, "stopped", observations,
            last_source, last_distance, 0.0, timeout,
            request.request_id)

    def _cancelled(self, request: _CheckpointRequest) -> bool:
        with self._condition:
            return bool(
                self._stop or request.generation != self._generation)

    @staticmethod
    def _failure(
            request: _CheckpointRequest, observations: int,
            source: tuple[float, float] | None, distance: float | None,
            elapsed: float, timeout: float,
            allowed_distance: float) -> CheckpointResult:
        if distance is not None and distance > allowed_distance:
            reason = "route_deviation"
        elif observations and source is None:
            reason = "coordinate_unavailable"
        else:
            reason = "checkpoint_timeout"
        return CheckpointResult(
            False, reason, observations, source, distance,
            elapsed, timeout, request.request_id)


def _distance(
        left: tuple[float, float], right: tuple[float, float]) -> float:
    return float(hypot(right[0] - left[0], right[1] - left[1]))
