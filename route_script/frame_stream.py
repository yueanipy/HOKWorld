'为路线回放提供单一后台最新帧。'
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from runtime_guard import dev_log


@dataclass(frozen=True)
class RouteFrame:
    '保存后台截图序号和只读图像引用。'

    sequence: int
    captured_at: float
    image: np.ndarray


class RouteFrameStream:
    '按当前消费者所需频率抓取最新帧，不参与键鼠回放。'

    CHECKPOINT_INTERVAL_S = 0.16
    DIAGNOSTIC_INTERVAL_S = 0.50

    def __init__(self, ctx, *, diagnostics: bool = False) -> None:
        self.ctx = ctx
        self._condition = threading.Condition()
        self._stop = False
        self._paused = False
        self._capturing = False
        self._diagnostics = bool(diagnostics)
        self._checkpoint_consumers = 0
        self._force_due = True
        self._sequence = 0
        self._latest: RouteFrame | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        '启动唯一截图线程。'
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="RouteFrameStream",
                daemon=True,
            )
            self._thread.start()

    def set_checkpoint_active(self, active: bool) -> None:
        '按检查点任务数量切换临时高频采样。'
        with self._condition:
            was_active = self._checkpoint_consumers > 0
            if active:
                self._checkpoint_consumers += 1
            else:
                self._checkpoint_consumers = max(
                    0, self._checkpoint_consumers - 1)
            if active and not was_active:
                self._force_due = True
            self._condition.notify_all()

    def latest(self, *, max_age: float | None = None) -> RouteFrame | None:
        '立即返回最新帧，不等待截图。'
        with self._condition:
            value = self._latest
        if value is None:
            return None
        if max_age is not None and time.monotonic() - value.captured_at > max_age:
            return None
        return value

    def wait_next(
            self, after_sequence: int, timeout: float) -> RouteFrame | None:
        '仅阻塞后台识别线程，等待比指定序号更新的帧。'
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while not self._stop:
                value = self._latest
                if value is not None and value.sequence > int(after_sequence):
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                self._condition.wait(remaining)
        return None

    def pause(self) -> None:
        '让传送、采集等独占视觉闭环安全使用截图器。'
        deadline = time.monotonic() + 1.0
        with self._condition:
            self._paused = True
            self._condition.notify_all()
            while self._capturing and not self._stop:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise RuntimeError("后台路线截图未及时让位")
                self._condition.wait(min(0.05, remaining))

    def resume(self) -> None:
        '恢复后台最新帧采样。'
        with self._condition:
            self._paused = False
            self._force_due = True
            self._condition.notify_all()

    def close(self) -> None:
        '停止截图线程并等待有界退出。'
        with self._condition:
            self._stop = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive():
                dev_log("[route] 后台路线截图线程未在3秒内结束")
        with self._condition:
            self._thread = None
            self._latest = None

    def _run(self) -> None:
        next_due = time.monotonic()
        while True:
            with self._condition:
                while not self._stop and (
                        self._paused
                        or not (self._diagnostics or self._checkpoint_consumers)):
                    self._condition.wait()
                    next_due = time.monotonic()
                if self._stop:
                    return
                interval = (
                    self.CHECKPOINT_INTERVAL_S
                    if self._checkpoint_consumers
                    else self.DIAGNOSTIC_INTERVAL_S)
                if self._force_due:
                    next_due = time.monotonic()
                    self._force_due = False
                wait_s = next_due - time.monotonic()
                if wait_s > 0.0:
                    self._condition.wait(wait_s)
                    continue
                self._capturing = True
            frame = None
            try:
                frame = self.ctx.grab_nowait()
            except Exception as exc:
                dev_log("[route] 后台路线截图异常", exc)
            captured_at = time.monotonic()
            with self._condition:
                self._capturing = False
                if (isinstance(frame, np.ndarray) and frame.ndim >= 2
                        and frame.size > 0):
                    self._sequence += 1
                    self._latest = RouteFrame(
                        self._sequence, captured_at, frame)
                next_due = captured_at + interval
                self._condition.notify_all()
