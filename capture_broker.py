'实时识别统一截图分发器。'
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from capture import GameCapture
from runtime_guard import dev_log


@dataclass(frozen=True)
class FrameSnapshot:
    sequence: int
    captured_at: float
    frame: np.ndarray | None


@dataclass
class _SubscriberState:
    name: str
    rois: tuple[tuple[float, float, float, float], ...]
    interval: float
    enabled: bool = True
    next_due: float = field(default_factory=time.monotonic)
    snapshot: FrameSnapshot | None = None
    requested_after_sequence: int | None = None


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

    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd)
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

    def _normalize_interval(self, interval: float) -> float:
        return max(self.MIN_INTERVAL, float(interval))

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
                state.rois = normalized_rois
                state.next_due = now
                state.snapshot = None
                state.requested_after_sequence = None
            if state.interval != interval:
                if interval < state.interval:
                    state.next_due = min(state.next_due, now + interval)
                state.interval = interval
            if not state.enabled:
                state.enabled = True
                state.next_due = now
                
                state.snapshot = None
                state.requested_after_sequence = None
            if state.snapshot is not None and state.snapshot.sequence > after_sequence:
                snapshot = state.snapshot
                state.snapshot = None
                return snapshot
            
            state.requested_after_sequence = after_sequence
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
                        due = [(token, state) for token, state in enabled if state.next_due <= now]
                        if due:
                            break
                        wait_s = min((state.next_due - now for _, state in enabled), default=0.5)
                        self._condition.wait(max(0.001, min(0.5, wait_s)))
                    if self._stopping:
                        return
                    rois = tuple(dict.fromkeys(roi for _, state in due for roi in state.rois))

                started = time.monotonic()
                frame = capture.grab_regions_canvas(rois)
                captured_at = time.monotonic()
                captures += 1

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
                        state.next_due = captured_at + state.interval
                    self._condition.notify_all()

                if captured_at - last_metrics >= 10.0:
                    elapsed = captured_at - last_metrics
                    dev_log(f"capture broker: {captures / elapsed:.1f} fps; "
                            f"last={1000 * (captured_at - started):.1f} ms; "
                            f"subs={len(self._subscribers)}")
                    captures = 0
                    last_metrics = captured_at
        except Exception as exc:
            dev_log("capture broker crashed", exc)
        finally:
            if capture is not None:
                capture.stop()
            with self._condition:
                self._stopping = True
                self._condition.notify_all()
            dev_log(f"capture broker stop: hwnd={self.hwnd}")


_BROKERS_LOCK = threading.Lock()
_BROKERS: dict[int, CaptureBroker] = {}


def subscribe_capture(hwnd: int, name: str, rois, interval: float) -> CaptureSubscription:
    '取得该窗口的共享实时截图订阅；失效的旧 Broker 会自动替换。'
    hwnd = int(hwnd)
    with _BROKERS_LOCK:
        broker = _BROKERS.get(hwnd)
        if broker is None or not broker.alive:
            broker = CaptureBroker(hwnd)
            _BROKERS[hwnd] = broker
        return broker.subscribe(name, rois, interval)
