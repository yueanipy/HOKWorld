'实时识别统一截图分发器。'
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from capture import GameCapture, RegionFrame
from runtime_guard import dev_log


@dataclass(frozen=True)
class FrameSnapshot:
    sequence: int
    captured_at: float
    frame: np.ndarray | RegionFrame | None


@dataclass
class _SubscriberState:
    name: str
    rois: tuple[tuple[float, float, float, float], ...]
    interval: float
    enabled: bool = True
    next_due: float = field(default_factory=time.monotonic)
    snapshot: FrameSnapshot | None = None
    requested_after_sequence: int | None = None
    requested_at: float | None = None


class CaptureSubscription:
    def __init__(self, broker: "CaptureBroker", token: int) -> None:
        self._broker = broker
        self._token = token
        self._seen_sequence = 0
        self._closed = False

    def get_frame(self, interval: float, rois, timeout: float = 1.0) -> FrameSnapshot | None:
        if self._closed:
            return None
        snapshot = self._broker._wait_frame(
            self._token, self._seen_sequence, interval, rois, timeout)
        if snapshot is not None:
            self._seen_sequence = snapshot.sequence
        return snapshot

    def set_enabled(self, enabled: bool) -> None:
        if not self._closed:
            self._broker._set_enabled(self._token, enabled)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker._unsubscribe(self._token)

    def __enter__(self) -> "CaptureSubscription":
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False


class CaptureBroker:
    MIN_INTERVAL = 0.015
    METRICS_INTERVAL = 10.0
    COALESCE_WINDOW = 0.006
    ROI_MERGE_MAX_INFLATION = 1.15

    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd)
        
        
        self._cadence_epoch = time.monotonic()
        self._condition = threading.Condition()
        self._subscribers: dict[int, _SubscriberState] = {}
        self._next_token = 1
        self._sequence = 0
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name=f"capture-broker-{self.hwnd}", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stopping

    def subscribe(self, name: str, rois, interval: float) -> CaptureSubscription:
        normalized = self._normalize_rois(rois)
        with self._condition:
            if self._stopping:
                raise RuntimeError("CaptureBroker is stopping")
            token = self._next_token
            self._next_token += 1
            self._subscribers[token] = _SubscriberState(
                name=name, rois=normalized, interval=self._normalize_interval(interval))
            self._condition.notify_all()
        return CaptureSubscription(self, token)

    @staticmethod
    def _normalize_rois(rois) -> tuple[tuple[float, float, float, float], ...]:
        return tuple(dict.fromkeys(tuple(float(v) for v in roi) for roi in rois))

    @staticmethod
    def _covers_rois(covering, requested) -> bool:
        '已有区域是否完整覆盖新区域；缩小截图范围时不需要额外抢跑一帧。'
        return all(any(
            outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3]
            for outer in covering
        ) for inner in requested)

    def _normalize_interval(self, interval: float) -> float:
        return max(self.MIN_INTERVAL, float(interval))

    def _next_cadence_due(self, now: float, interval: float) -> float:
        '返回公共相位上严格晚于 now 的下一个订阅截止时间。'
        interval = self._normalize_interval(interval)
        elapsed = max(0.0, float(now) - self._cadence_epoch)
        slot = math.floor((elapsed + 1e-9) / interval) + 1
        return self._cadence_epoch + slot * interval

    @classmethod
    def _merge_rois(cls, rois) -> tuple[tuple[float, float, float, float], ...]:
        '合并重叠或紧邻 ROI，同时限制外接矩形的额外面积。'
        merged = [tuple(roi) for roi in dict.fromkeys(rois)]
        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                ax0, ay0, ax1, ay1 = merged[i]
                area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
                for j in range(i + 1, len(merged)):
                    bx0, by0, bx1, by1 = merged[j]
                    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
                    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
                    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
                    union_area = area_a + area_b - ix * iy
                    bounds = (min(ax0, bx0), min(ay0, by0),
                              max(ax1, bx1), max(ay1, by1))
                    bounds_area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                    if union_area > 0.0 and bounds_area <= union_area * cls.ROI_MERGE_MAX_INFLATION:
                        merged[i] = bounds
                        merged.pop(j)
                        changed = True
                        break
                if changed:
                    break
        return tuple(merged)

    def _wait_frame(self, token: int, after_sequence: int, interval: float,
                    rois, timeout: float) -> FrameSnapshot | None:
        normalized_rois = self._normalize_rois(rois)
        interval = self._normalize_interval(interval)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            if self._stopping:
                raise RuntimeError("CaptureBroker 已停止")
            state = self._subscribers.get(token)
            if state is None:
                return None
            now = time.monotonic()
            if state.rois != normalized_rois:
                expanded = not self._covers_rois(state.rois, normalized_rois)
                state.rois = normalized_rois
                if expanded:
                    state.next_due = now
                state.snapshot = None
                state.requested_after_sequence = None
                state.requested_at = None
            if state.interval != interval:
                if interval < state.interval:
                    
                    state.next_due = min(
                        state.next_due, self._next_cadence_due(now, interval))
                else:
                    
                    state.next_due = self._next_cadence_due(now, interval)
                state.interval = interval
            if not state.enabled:
                state.enabled = True
                state.next_due = now
                
                state.snapshot = None
                state.requested_after_sequence = None
                state.requested_at = None
            if state.snapshot is not None and state.snapshot.sequence > after_sequence:
                snapshot = state.snapshot
                state.snapshot = None
                return snapshot
            
            state.requested_after_sequence = after_sequence
            state.requested_at = time.monotonic()
            self._condition.notify_all()
            while not self._stopping:
                state = self._subscribers.get(token)
                if state is None:
                    return None
                if state.snapshot is not None and state.snapshot.sequence > after_sequence:
                    snapshot = state.snapshot
                    state.snapshot = None
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if state.requested_after_sequence == after_sequence:
                        state.requested_after_sequence = None
                        state.requested_at = None
                    return None
                self._condition.wait(remaining)
        raise RuntimeError("CaptureBroker 已停止")

    def _set_enabled(self, token: int, enabled: bool) -> None:
        with self._condition:
            state = self._subscribers.get(token)
            if state is None or state.enabled == bool(enabled):
                return
            state.enabled = bool(enabled)
            if state.enabled:
                state.next_due = time.monotonic()
            else:
                state.snapshot = None
                state.requested_after_sequence = None
                state.requested_at = None
            self._condition.notify_all()

    def _unsubscribe(self, token: int) -> None:
        with self._condition:
            self._subscribers.pop(token, None)
            if not self._subscribers:
                self._stopping = True
            self._condition.notify_all()

    def _run(self) -> None:
        capture = None
        captures = 0
        capture_total_ms = 0.0
        capture_max_ms = 0.0
        geometry_total_ms = 0.0
        blit_total_ms = 0.0
        compose_total_ms = 0.0
        copy_total_ms = 0.0
        schedule_total_ms = 0.0
        schedule_max_ms = 0.0
        publish_total_ms = 0.0
        regions_total = 0
        raw_regions_total = 0
        due_subscribers_total = 0
        last_metrics = time.monotonic()
        try:
            
            
            try:
                import win32api
                import win32process
                win32process.SetThreadPriority(
                    win32api.GetCurrentThread(), win32process.THREAD_PRIORITY_BELOW_NORMAL)
            except Exception as exc:
                dev_log("capture broker: 设置低线程优先级失败，继续运行", exc)
            capture = GameCapture(self.hwnd)
            capture.start()
            dev_log(f"capture broker start: hwnd={self.hwnd}")
            while True:
                with self._condition:
                    due: list[tuple[int, _SubscriberState]] = []
                    while not due and not self._stopping:
                        now = time.monotonic()
                        enabled = [(token, state) for token, state in self._subscribers.items()
                                   if (state.enabled and state.rois
                                       and state.requested_after_sequence is not None)]
                        if not enabled:
                            self._condition.wait(0.5)
                            continue
                        
                        
                        earliest_next_due = min(state.next_due for _, state in enabled)
                        if earliest_next_due > now:
                            
                            
                            
                            sleep_s = min(0.05, earliest_next_due - now)
                            self._condition.release()
                            try:
                                time.sleep(sleep_s)
                            finally:
                                self._condition.acquire()
                            continue
                        due = [(token, state) for token, state in enabled
                               if state.next_due <= now + self.COALESCE_WINDOW]
                        if due:
                            break
                    if self._stopping:
                        return
                    raw_rois = tuple(dict.fromkeys(roi for _, state in due for roi in state.rois))
                    rois = self._merge_rois(raw_rois)
                    
                    
                    earliest_due = min(max(state.next_due, state.requested_at or state.next_due)
                                       for _, state in due)

                started = time.monotonic()
                schedule_ms = 1000.0 * max(0.0, started - earliest_due)
                frame = capture.grab_regions_compact(rois)
                captured_at = time.monotonic()
                capture_ms = 1000.0 * (captured_at - started)
                captures += 1
                capture_total_ms += capture_ms
                capture_max_ms = max(capture_max_ms, capture_ms)
                schedule_total_ms += schedule_ms
                schedule_max_ms = max(schedule_max_ms, schedule_ms)
                timing = capture.last_region_metrics
                geometry_total_ms += timing.geometry_ms
                blit_total_ms += timing.blit_ms
                compose_total_ms += timing.compose_ms
                copy_total_ms += timing.copy_ms
                regions_total += timing.regions
                raw_regions_total += len(raw_rois)
                due_subscribers_total += len(due)

                publish_started = time.monotonic()
                with self._condition:
                    self._sequence += 1
                    snapshot = FrameSnapshot(self._sequence, captured_at, frame)
                    due_tokens = {token for token, _ in due}
                    for token in due_tokens:
                        state = self._subscribers.get(token)
                        if (state is None or not state.enabled
                                or state.requested_after_sequence is None):
                            continue
                        state.snapshot = snapshot
                        state.requested_after_sequence = None
                        state.requested_at = None
                        
                        
                        state.next_due = self._next_cadence_due(
                            captured_at, state.interval)
                    subscriber_count = len(self._subscribers)
                    self._condition.notify_all()
                publish_total_ms += 1000.0 * (time.monotonic() - publish_started)

                if captured_at - last_metrics >= self.METRICS_INTERVAL:
                    elapsed = captured_at - last_metrics
                    count = max(1, captures)
                    dev_log(f"capture broker: {captures / elapsed:.1f} fps; "
                            f"capture avg/max/last={capture_total_ms / count:.1f}/"
                            f"{capture_max_ms:.1f}/{capture_ms:.1f} ms; "
                            f"stage avg geometry/blit/compose/copy="
                            f"{geometry_total_ms / count:.2f}/{blit_total_ms / count:.2f}/"
                            f"{compose_total_ms / count:.2f}/{copy_total_ms / count:.2f} ms; "
                            f"schedule avg/max={schedule_total_ms / count:.2f}/"
                            f"{schedule_max_ms:.2f} ms; publish avg="
                            f"{publish_total_ms / count:.2f} ms; "
                            f"rois raw/merged avg={raw_regions_total / count:.1f}/"
                            f"{regions_total / count:.1f}; due subs avg="
                            f"{due_subscribers_total / count:.1f}; subs={subscriber_count}")
                    captures = 0
                    capture_total_ms = 0.0
                    capture_max_ms = 0.0
                    geometry_total_ms = 0.0
                    blit_total_ms = 0.0
                    compose_total_ms = 0.0
                    copy_total_ms = 0.0
                    schedule_total_ms = 0.0
                    schedule_max_ms = 0.0
                    publish_total_ms = 0.0
                    regions_total = 0
                    raw_regions_total = 0
                    due_subscribers_total = 0
                    last_metrics = captured_at
        except Exception as exc:
            dev_log("capture broker crashed", exc)
        finally:
            if capture is not None:
                capture.stop()
            with self._condition:
                self._stopping = True
                self._condition.notify_all()
            _discard_broker(self.hwnd, self)
            dev_log(f"capture broker stop: hwnd={self.hwnd}")


_BROKERS_LOCK = threading.Lock()
_BROKERS: dict[int, CaptureBroker] = {}


def _discard_broker(hwnd: int, broker: CaptureBroker) -> None:
    '仅在注册项仍指向当前实例时移除已停止的截图分发器。'
    with _BROKERS_LOCK:
        if _BROKERS.get(int(hwnd)) is broker:
            _BROKERS.pop(int(hwnd), None)


def subscribe_capture(hwnd: int, name: str, rois, interval: float) -> CaptureSubscription:
    '取得该窗口的共享实时截图订阅；失效的旧 Broker 会自动替换。'
    hwnd = int(hwnd)
    with _BROKERS_LOCK:
        broker = _BROKERS.get(hwnd)
        if broker is None or not broker.alive:
            broker = CaptureBroker(hwnd)
            _BROKERS[hwnd] = broker
        return broker.subscribe(name, rois, interval)
