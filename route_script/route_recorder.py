'仅记录游戏前台键鼠与稀疏坐标的路线录制器。'
from __future__ import annotations

import ctypes
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from pynput import keyboard, mouse

from capture import GameCapture
from winenv import client_rect_on_screen, find_game_hwnd, is_foreground
from runtime_guard import dev_log
from world_map.core import WorldMapAtlas
from world_map.minimap import MiniMapPoseRecognizer, MiniMapPoseTracker

from .compiler import (
    RELATIVE_MOUSE_MERGE_WINDOW_S,
    CoordinateSample,
    RawRouteEvent,
    compile_recording,
    normalize_key,
)
from .map_semantics import RecordedTeleportObserver
from .model import (
    ROUTE_CONTROL_KEYS,
    RouteEvent,
    RouteMetadata,
    RouteScript,
    SUPPORTED_KEYS,
)
from .raw_input import RawMouseMonitor
from .store import RouteStore
from .visual_anchor import (
    VisualAnchor,
    build_visual_anchor,
    replace_visual_anchor_set,
)



Log = Callable[[str], None]
StateCallback = Callable[[str], None]


class RouteRecorder:
    '录制一段固定设备路线，不保存连续视频帧。'

    SAMPLE_INTERVAL = 0.20
    POSITION_INTERVAL = 0.60
    VISUAL_ANCHOR_INTERVAL = 2.50
    MAX_VISUAL_ANCHORS = 256
    MAX_VISUAL_ANCHOR_BYTES = 8 * 1024 * 1024
    MAX_COMPILED_EVENTS = 100_000
    MAX_RAW_EVENTS = 100_000
    CRITICAL_EVENT_RESERVE = 256
    MAX_COORDINATE_SAMPLES = 50_000
    CONTROL_KEYS = ROUTE_CONTROL_KEYS
    RELATIVE_MOUSE_SETTLE = 0.35
    RAW_INPUT_SETTLE = 0.08
    HUD_EXIT_CONFIRM_SAMPLES = 5

    def __init__(
            self, name: str, *, start_teleport: str = "",
            coordinate_correction: bool = False,
            store: RouteStore | None = None, log: Log = print,
            on_state: StateCallback | None = None) -> None:
        self.name = RouteStore.normalize_name(name)
        self.start_teleport = str(start_teleport).strip()
        self.coordinate_correction = bool(coordinate_correction)
        self.store = store or RouteStore()
        self.log = log
        self.on_state = on_state or (lambda _text: None)
        self._stop = threading.Event()
        self._cancel = False
        self._running = False
        self._lock = threading.RLock()
        self._raw: list[RawRouteEvent] = []
        self._coordinates: list[CoordinateSample] = []
        self._visual_anchors: list[tuple[float, VisualAnchor]] = []
        self._visual_anchor_bytes = 0
        self._visual_anchor_interval = self.VISUAL_ANCHOR_INTERVAL
        self._visual_anchor_compactions = 0
        self._keys_down: set[str] = set()
        self._ignored_keyups: set[str] = set()
        self._mouse_buttons: set[str] = set()
        self._mouse_positioned: dict[str, bool] = {}
        self._last_pointer: tuple[int, int] | None = None
        self._hud_mode = False
        self._hud_miss_count = 0
        self._order = 0
        self._started_at = 0.0
        self._paused_started: float | None = None
        self._paused_total = 0.0
        self._client = (0, 0, 0, 0)
        self._hwnd: int | None = None
        self._kb: keyboard.Listener | None = None
        self._mouse: mouse.Listener | None = None
        self._raw_mouse: RawMouseMonitor | None = None
        self._raw_mouse_enabled = False
        self._relative_record_ready_at = float("inf")
        self._atlas: WorldMapAtlas | None = None
        self._overflow_error = ""
        self._map_observer = RecordedTeleportObserver()

    def stop(self, *, cancel: bool = False) -> None:
        self._cancel = self._cancel or bool(cancel)
        self._stop.set()

    def record(self) -> Path | None:
        '阻塞录制直到stop，并在正常结束时原子保存TXT和JSON。'
        if self._stop.is_set():
            self.log("路线录制已在启动前停止")
            return None
        hwnd = find_game_hwnd(prefer_foreground=True)
        if not hwnd:
            raise RuntimeError("未找到游戏窗口")
        self._hwnd = hwnd
        self._client = client_rect_on_screen(hwnd)
        if self._client[2] <= 0 or self._client[3] <= 0:
            raise RuntimeError("游戏客户区尺寸无效")
        self._started_at = time.monotonic()
        cap: GameCapture | None = None
        recognizer: MiniMapPoseRecognizer | None = None
        tracker: MiniMapPoseTracker | None = None
        last_position_at = -999.0
        last_visual_anchor_at = -999.0
        foreground_logged = True
        try:
            if self._stop.is_set():
                self.log("路线录制已在启动前停止")
                return None



            needs_vision = True
            if self.coordinate_correction:
                self._atlas = WorldMapAtlas()
                recognizer = MiniMapPoseRecognizer(self._atlas)
                tracker = MiniMapPoseTracker(
                    window=5, required=2, max_atlas_jump=85.0)
            else:

                recognizer = MiniMapPoseRecognizer()
            if self._stop.is_set():
                self.log("路线录制已在启动前停止")
                return None
            if needs_vision:
                cap = GameCapture(hwnd)
                cap.start()
                initial_frame = cap.grab()
                if initial_frame is not None and recognizer is not None:
                    try:
                        initial_pose = recognizer.detect(initial_frame, locate=False)
                    except Exception as exc:
                        dev_log("[route record] 初始HUD识别异常", exc)
                        initial_pose = None
                    self._set_hud_mode(bool(
                        initial_pose is not None
                        and initial_pose.character_heading_deg is not None))

            self._started_at = time.monotonic()
            self._running = True
            self._start_listeners()
            self.on_state("录制中")
            self.log("路线录制已开始；F12结束并保存，切出游戏时暂停记录")
            first_sample = True
            while True:


                if not first_sample and self._stop.wait(self.SAMPLE_INTERVAL):
                    break
                first_sample = False
                if self._stop.is_set():
                    break
                if not find_game_hwnd():
                    raise RuntimeError("游戏窗口已关闭")
                if not is_foreground(hwnd):
                    if foreground_logged:
                        foreground_logged = False
                        self._begin_pause()
                        self.on_state("游戏不在前台，录制已暂停")
                    continue
                if not foreground_logged:
                    foreground_logged = True
                    self._end_pause()
                    if tracker is not None:
                        tracker.reset()
                    self._last_pointer = None
                    self.on_state("录制中")
                if cap is None or recognizer is None:
                    self._set_hud_mode(True)
                    continue
                frame = cap.grab()
                if frame is None:
                    continue
                now = self._now()
                sample_position = bool(
                    self.coordinate_correction
                    and now - last_position_at >= self.POSITION_INTERVAL)
                if sample_position:

                    last_position_at = now
                try:
                    heading_pose = recognizer.detect(frame, locate=sample_position)
                except Exception as exc:
                    dev_log("[route record] 小地图识别异常", exc)
                    heading_pose = None
                immediate_hud = bool(
                    heading_pose is not None
                    and heading_pose.character_heading_deg is not None)
                self._set_hud_mode(immediate_hud)
                self._map_observer.update(frame, now, hud=immediate_hud)
                if (self.coordinate_correction
                        and heading_pose is not None
                        and heading_pose.character_heading_deg is not None):
                    self._record_movement_heading(
                        now, heading_pose.character_heading_deg)
                if (self.coordinate_correction and immediate_hud
                        and now - last_visual_anchor_at
                        >= self._visual_anchor_interval):

                    last_visual_anchor_at = now
                    try:
                        visual_anchor = build_visual_anchor(frame)
                    except Exception as exc:
                        dev_log("[route record] 小地图视觉锚点生成异常", exc)
                        visual_anchor = None
                    if visual_anchor is not None:
                        self._append_visual_anchor(now, visual_anchor)
                if sample_position and self._hud_mode and tracker is not None:
                    try:
                        pose = tracker.update(heading_pose)
                    except Exception as exc:
                        dev_log("[route record] 小地图坐标采样异常", exc)
                        pose = None
                    if pose is not None and pose.source is not None:
                        movement = self._movement_mode()
                        with self._lock:
                            if len(self._coordinates) >= self.MAX_COORDINATE_SAMPLES:
                                self._mark_overflow_locked("坐标样本超过安全上限")
                            else:
                                self._coordinates.append(CoordinateSample(
                                    now, pose.source[0], pose.source[1], movement))
        finally:
            self._running = False
            if cap is not None:
                try:
                    cap.stop()
                except Exception as exc:
                    dev_log("[route record] 截图器停止失败", exc)
            self._stop_listeners()
            self._end_pause()

        if self._overflow_error:
            raise RuntimeError(self._overflow_error)
        if self._cancel:
            self.log("路线录制已取消")
            return None
        route = self._build_route()
        path = self.store.save(route, overwrite=True)
        try:
            anchor_files = tuple(
                (f"anchor_{index:04d}.npz", anchor)
                for index, (_at, anchor) in enumerate(self._visual_anchors)
            )
            replace_visual_anchor_set(
                self.store.directory, self.name, anchor_files)
        except Exception as exc:

            dev_log("[route record] 视觉锚点保存失败，回退纯键鼠路线", exc)
            route = RouteScript(
                route.metadata,
                tuple(event for event in route.events
                      if event.action != "visual_checkpoint"),
            )
            path = self.store.save(route, overwrite=True)
        heading_count = sum(
            event.action == "heading_anchor" for event in route.events)
        move_count = sum(
            event.action == "mouse_move_rel" for event in route.events)
        checkpoint_count = sum(
            event.action == "checkpoint" for event in route.events)
        visual_count = sum(
            event.action == "visual_checkpoint" for event in route.events)
        teleport_count = sum(
            event.action == "teleport" for event in route.events)
        self.log(
            f"路线录制数据：镜头片段 {move_count}，方向锚点 {heading_count}，"
            f"坐标节点 {checkpoint_count}，视觉节点 {visual_count}，"
            f"图谱传送 {teleport_count}")
        self.log(f"路线录制已保存：{path.name}")
        return path

    def _build_route(self) -> RouteScript:
        x, y, width, height = self._client
        del x, y
        with self._lock:
            raw = tuple(self._raw)
            coordinates = tuple(self._coordinates)
            visual_anchors = tuple(self._visual_anchors)
        events = compile_recording(
            raw,
            coordinate_samples=coordinates if self.coordinate_correction else (),
            teleport_segments=self._map_observer.segments(),
            stopped_at=self._now(),
        )
        visual_capacity = max(
            0, self.MAX_COMPILED_EVENTS - len(events))
        visual_anchors = self._sparsify_visual_anchors(
            visual_anchors,
            min(self.MAX_VISUAL_ANCHORS, visual_capacity),
        )

        with self._lock:
            self._visual_anchors = list(visual_anchors)
            self._visual_anchor_bytes = sum(
                self._visual_anchor_size(anchor)
                for _at, anchor in visual_anchors
            )
        if self.coordinate_correction and visual_anchors:
            visual_events = tuple(
                RouteEvent(float(at), "visual_checkpoint", {
                    "asset": f"anchors/anchor_{index:04d}.npz",
                    "max_offset": 14.0,
                    "retry": 2,
                })
                for index, (at, _anchor) in enumerate(visual_anchors)
            )
            events = tuple(sorted(
                (*events, *visual_events),
                key=lambda event: float(event.at),
            ))
        if not events:
            events = (RouteEvent(0.0, "snapshot", {"name": "空路线"}),)
        metadata = RouteMetadata(
            name=self.name,
            start_teleport=self.start_teleport,
            client_width=width,
            client_height=height,
            dpi_scale=self._dpi_scale(),
            coordinate_correction=self.coordinate_correction,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        route = RouteScript(metadata, events)
        route.validate()
        return route

    def _append_visual_anchor(
            self, at: float, anchor: VisualAnchor) -> bool:
        '有界保留视觉锚点，长路线按时间均匀逐级稀疏。'
        size = self._visual_anchor_size(anchor)
        if size <= 0 or size > self.MAX_VISUAL_ANCHOR_BYTES:
            return False
        with self._lock:
            while self._visual_anchors and (
                    len(self._visual_anchors) >= self.MAX_VISUAL_ANCHORS
                    or self._visual_anchor_bytes + size
                    > self.MAX_VISUAL_ANCHOR_BYTES):
                before = len(self._visual_anchors)
                self._visual_anchors = self._visual_anchors[::2]
                self._visual_anchor_bytes = sum(
                    self._visual_anchor_size(item)
                    for _at, item in self._visual_anchors
                )
                self._visual_anchor_interval *= 2.0
                self._visual_anchor_compactions += 1
                if len(self._visual_anchors) >= before:
                    return False
            self._visual_anchors.append((float(at), anchor))
            self._visual_anchor_bytes += size
            return True

    @staticmethod
    def _visual_anchor_size(anchor: VisualAnchor) -> int:
        '返回锚点数组占用的字节数。'
        try:
            return int(anchor.points.nbytes + anchor.descriptors.nbytes)
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _sparsify_visual_anchors(
            anchors: tuple[tuple[float, VisualAnchor], ...],
            limit: int) -> tuple[tuple[float, VisualAnchor], ...]:
        '均匀缩减锚点，同时保留路线首尾覆盖。'
        count = len(anchors)
        limit = max(0, int(limit))
        if count <= limit:
            return anchors
        if limit == 0:
            return ()
        if limit == 1:
            return (anchors[count // 2],)
        indices = tuple(
            round(index * (count - 1) / (limit - 1))
            for index in range(limit)
        )
        return tuple(anchors[index] for index in indices)

    def _start_listeners(self) -> None:
        '启动键鼠监听，镜头移动优先使用 Raw Input。'
        self._kb = keyboard.Listener(
            on_press=self._on_key_down, on_release=self._on_key_up)
        try:
            self._kb.start()
            try:
                self._raw_mouse = RawMouseMonitor(
                    self._on_raw_mouse_move,
                    error_log=lambda message, exc: dev_log(
                        f"[route record] {message}", exc),
                )
                self._raw_mouse.start()
                self._raw_mouse_enabled = True
                self._relative_record_ready_at = (
                    time.monotonic() + self.RAW_INPUT_SETTLE)
                self.log("路线镜头录制使用 Raw Input 相对增量")
            except Exception as exc:
                self._raw_mouse = None
                self._raw_mouse_enabled = False
                dev_log("[route record] Raw Input不可用，回退光标差值", exc)
                self.log("Raw Input不可用，镜头录制已使用兼容模式")

            mouse_callbacks = {
                "on_click": self._on_click,
                "on_scroll": self._on_scroll,
            }
            if not self._raw_mouse_enabled:
                mouse_callbacks["on_move"] = self._on_move
            self._mouse = mouse.Listener(**mouse_callbacks)
            self._mouse.start()
        except Exception:
            self._stop_listeners()
            raise

    def _stop_listeners(self) -> None:
        for listener in (self._kb, self._mouse):
            if listener is None:
                continue
            try:
                listener.stop()
                listener.join(timeout=1.0)
            except Exception as exc:
                dev_log("[route record] 监听器停止失败", exc)
        self._kb = None
        self._mouse = None
        raw_mouse = self._raw_mouse
        self._raw_mouse = None
        self._raw_mouse_enabled = False
        if raw_mouse is not None:
            try:
                raw_mouse.stop()
            except Exception as exc:
                dev_log("[route record] Raw Input监听器停止失败", exc)

    def _record(self, kind: str, data: dict, *, at: float | None = None) -> None:
        if not self._running or not self._event_allowed():
            return
        with self._lock:
            self._append_raw_locked(
                self._now() if at is None else float(at), kind, data,
                critical=kind != "mouse_move_rel",
            )

    def _append_raw_locked(
            self, at: float, kind: str, data: dict, *, critical: bool) -> bool:
        '有界追加；先为按键抬起等关键事件预留空间。'
        if not critical and kind == "mouse_move_rel" and self._raw:
            previous = self._raw[-1]
            previous_end = float(previous.data.get("end_at", previous.at))
            current_at = float(at)
            dx = int(previous.data.get("dx", 0)) + int(data.get("dx", 0))
            dy = int(previous.data.get("dy", 0)) + int(data.get("dy", 0))
            if (previous.kind == kind
                    and previous_end <= current_at
                    and current_at - float(previous.at)
                    <= RELATIVE_MOUSE_MERGE_WINDOW_S
                    and abs(dx) <= 10_000 and abs(dy) <= 10_000):
                merged = dict(previous.data)
                merged.update({"dx": dx, "dy": dy, "end_at": current_at})
                self._raw[-1] = RawRouteEvent(
                    previous.at, previous.kind, merged, previous.order)
                return True
        reserve = max(0, min(self.CRITICAL_EVENT_RESERVE, self.MAX_RAW_EVENTS))
        soft_limit = max(0, self.MAX_RAW_EVENTS - reserve)
        if not critical and len(self._raw) >= soft_limit:
            self._mark_overflow_locked("镜头移动事件超过安全上限")
            return False
        if len(self._raw) >= self.MAX_RAW_EVENTS:
            self._mark_overflow_locked("路线事件超过安全上限")
            return False
        self._order += 1
        self._raw.append(RawRouteEvent(float(at), kind, dict(data), self._order))
        return True

    def _mark_overflow_locked(self, message: str) -> None:
        if not self._overflow_error:
            self._overflow_error = str(message)
            self._stop.set()

    def _event_allowed(self) -> bool:
        hwnd = self._hwnd
        return bool(hwnd and is_foreground(hwnd) and not self._stop.is_set())

    def _on_key_down(self, key) -> None:
        name = normalize_key(_key_name(key))
        if (not self._event_allowed() or name in self.CONTROL_KEYS
                or name not in SUPPORTED_KEYS):
            return
        with self._lock:
            if name in self._keys_down:
                return
            self._keys_down.add(name)
        at = self._now()
        if name == "m":
            self._map_observer.note_map_key(at)
        self._record("key_down", {"key": name}, at=at)

    def _on_key_up(self, key) -> None:
        name = normalize_key(_key_name(key))
        if not self._event_allowed():
            with self._lock:
                was_down = name in self._keys_down
                ignored = name in self._ignored_keyups
                self._keys_down.discard(name)
                self._ignored_keyups.discard(name)
                if (was_down and not ignored and name not in self.CONTROL_KEYS
                        and name in SUPPORTED_KEYS):
                    self._append_raw_locked(
                        self._now(), "key_up", {"key": name}, critical=True)
            return
        with self._lock:
            was_down = name in self._keys_down
            self._keys_down.discard(name)
            if name in self._ignored_keyups:
                self._ignored_keyups.discard(name)
                return
        if (not was_down or name in self.CONTROL_KEYS
                or name not in SUPPORTED_KEYS):
            return
        self._record("key_up", {"key": name})

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        name = getattr(button, "name", str(button)).lower()
        if name not in {"left", "right", "middle"}:
            return
        if not self._event_allowed():
            with self._lock:
                was_pressed = name in self._mouse_buttons
                self._mouse_buttons.discard(name)
                positioned = self._mouse_positioned.pop(
                    name, not self._hud_mode)
                if not pressed and was_pressed:
                    px, py = self._last_pointer or (
                        self._client[0] + self._client[2] // 2,
                        self._client[1] + self._client[3] // 2,
                    )
                    nx, ny = self._normalize_pointer(px, py)
                    self._append_raw_locked(
                        self._now(), "mouse_up",
                        {
                            "button": name, "x": nx, "y": ny,
                            "positioned": positioned,
                        }, critical=True)
            return
        if not self._pointer_inside(x, y):
            with self._lock:
                was_pressed = name in self._mouse_buttons
                self._mouse_buttons.discard(name)
                positioned = self._mouse_positioned.pop(
                    name, not self._hud_mode)
                if not pressed and was_pressed:
                    px, py = self._last_pointer or (
                        self._client[0] + self._client[2] // 2,
                        self._client[1] + self._client[3] // 2,
                    )
                    nx, ny = self._normalize_pointer(px, py)
                    self._append_raw_locked(
                        self._now(), "mouse_up",
                        {
                            "button": name, "x": nx, "y": ny,
                            "positioned": positioned,
                        }, critical=True)
            return
        nx, ny = self._normalize_pointer(x, y)
        at = self._now()
        with self._lock:
            self._last_pointer = (int(x), int(y))
            if pressed:
                positioned = not self._hud_mode
                self._mouse_buttons.add(name)
                self._mouse_positioned[name] = positioned
            else:
                self._mouse_buttons.discard(name)
                positioned = self._mouse_positioned.pop(
                    name, not self._hud_mode)
        self._record("mouse_down" if pressed else "mouse_up", {
            "button": name, "x": nx, "y": ny,
            "positioned": positioned,
        }, at=at)
        self._map_observer.note_click(
            at, nx, ny, button=name, pressed=bool(pressed))

    def _on_scroll(self, x: int, y: int, _dx: int, dy: int) -> None:
        if not self._event_allowed() or not self._pointer_inside(x, y):
            return
        nx, ny = self._normalize_pointer(x, y)
        with self._lock:
            self._last_pointer = (int(x), int(y))
            positioned = not self._hud_mode
        self._record("mouse_wheel", {
            "notches": int(dy), "x": nx, "y": ny,
            "positioned": positioned,
        })

    def _on_move(self, x: int, y: int) -> None:
        '兼容模式下按屏幕光标差值记录相对镜头移动。'
        if not self._event_allowed():
            return
        if not self._pointer_inside(x, y):
            with self._lock:
                self._last_pointer = None
            return
        with self._lock:
            previous = self._last_pointer
            self._last_pointer = (int(x), int(y))
            hud_mode = self._hud_mode
            ready_at = self._relative_record_ready_at
        if (not hud_mode or previous is None
                or time.monotonic() < ready_at):
            return
        dx, dy = int(x) - previous[0], int(y) - previous[1]
        width, height = max(1, self._client[2]), max(1, self._client[3])
        if abs(dx) > width * 0.25 or abs(dy) > height * 0.25:
            return
        if dx or dy:
            self._record("mouse_move_rel", {"dx": dx, "dy": dy})

    def _record_movement_heading(self, at: float, degrees: float) -> None:
        '在持续前进时记录角色真实朝向，供回放闭环纠偏。'
        with self._lock:
            if "w" not in self._keys_down:
                return
            self._append_raw_locked(
                float(at), "heading_anchor", {
                    "degrees": float(degrees) % 360.0,
                    "tolerance": 7.0,
                },
                critical=False,
            )

    def _on_raw_mouse_move(self, dx: int, dy: int) -> None:
        '记录 Raw Input 提供的真实相对镜头增量。'
        if not self._event_allowed():
            return
        with self._lock:
            hud_mode = self._hud_mode
            ready_at = self._relative_record_ready_at
        if not hud_mode or time.monotonic() < ready_at:
            return
        dx, dy = int(dx), int(dy)
        if (dx or dy) and abs(dx) <= 10_000 and abs(dy) <= 10_000:
            self._record("mouse_move_rel", {"dx": dx, "dy": dy})

    def _set_hud_mode(self, enabled: bool) -> None:
        '切换 HUD 模式并过滤刚进入时的鼠标回中噪声。'
        enabled = bool(enabled)
        now = time.monotonic()
        with self._lock:
            if enabled:
                self._hud_miss_count = 0
            elif self._hud_mode:
                self._hud_miss_count += 1
                if self._hud_miss_count < self.HUD_EXIT_CONFIRM_SAMPLES:
                    return
            else:
                self._hud_miss_count = 0
            if enabled and not self._hud_mode:
                if self._raw_mouse_enabled:
                    if self._relative_record_ready_at == float("inf"):
                        self._relative_record_ready_at = now
                else:
                    self._relative_record_ready_at = (
                        now + self.RELATIVE_MOUSE_SETTLE)
                self._last_pointer = None
            elif not enabled:
                self._relative_record_ready_at = float("inf")
                self._last_pointer = None
                self._hud_miss_count = 0
            self._hud_mode = enabled

    def _normalize_pointer(self, x: int, y: int) -> tuple[float, float]:
        left, top, width, height = self._client
        if width <= 0 or height <= 0:
            return 0.5, 0.5
        return (
            max(0.0, min(1.0, (int(x) - left) / width)),
            max(0.0, min(1.0, (int(y) - top) / height)),
        )

    def _pointer_inside(self, x: int, y: int) -> bool:
        left, top, width, height = self._client
        return bool(
            width > 0 and height > 0
            and left <= int(x) < left + width
            and top <= int(y) < top + height)

    def _movement_mode(self) -> str:
        with self._lock:
            keys = set(self._keys_down)
        if "shift" in keys:
            return "run"
        return "walk"

    def _begin_pause(self) -> None:
        if self._paused_started is None:
            self._map_observer.cancel()
            now = self._now()
            with self._lock:
                for key in sorted(self._keys_down):
                    if key not in self._ignored_keyups:
                        self._append_raw_locked(
                            now, "key_up", {"key": key}, critical=True)
                self._keys_down.clear()
                self._ignored_keyups.clear()
                for button in sorted(self._mouse_buttons):
                    nx, ny = self._normalize_pointer(
                        *(self._last_pointer or (
                            self._client[0] + self._client[2] // 2,
                            self._client[1] + self._client[3] // 2)))
                    self._append_raw_locked(
                        now, "mouse_up",
                        {
                            "button": button, "x": nx, "y": ny,
                            "positioned": self._mouse_positioned.get(
                                button, not self._hud_mode),
                        },
                        critical=True,
                    )
                self._mouse_buttons.clear()
                self._mouse_positioned.clear()
                self._hud_mode = False
                self._hud_miss_count = 0
                self._relative_record_ready_at = float("inf")
                self._last_pointer = None
            self._paused_started = time.monotonic()

    def _end_pause(self) -> None:
        if self._paused_started is not None:
            self._paused_total += time.monotonic() - self._paused_started
            self._paused_started = None
            with self._lock:
                self._relative_record_ready_at = (
                    time.monotonic()
                    if self._raw_mouse_enabled else
                    time.monotonic() + self.RELATIVE_MOUSE_SETTLE)
                self._last_pointer = None

    def _now(self) -> float:
        now = time.monotonic()
        paused = (
            now - self._paused_started if self._paused_started is not None else 0.0)
        return max(0.0, now - self._started_at - self._paused_total - paused)

    def _dpi_scale(self) -> float:
        try:
            dpi = int(ctypes.windll.user32.GetDpiForWindow(int(self._hwnd or 0)))
            return max(0.5, min(4.0, dpi / 96.0))
        except Exception:
            return 1.0


def _key_name(key) -> str:
    try:
        if getattr(key, "char", None) is not None:
            return str(key.char)
        return str(key.name)
    except Exception:
        return str(key)
