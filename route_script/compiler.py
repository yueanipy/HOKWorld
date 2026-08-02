'把录制器原始事件编译为紧凑安全的路线时间轴。'
from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot
from typing import Any, Iterable

from .model import RouteEvent




RELATIVE_MOUSE_MERGE_WINDOW_S = 0.020


@dataclass(frozen=True)
class RawRouteEvent:
    '保存录制回调产生的原始事件。'

    at: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    order: int = 0


@dataclass(frozen=True)
class CoordinateSample:
    '保存一次可信的小地图坐标样本。'

    at: float
    x: float
    y: float
    movement: str = "walk"


@dataclass(frozen=True)
class TeleportSegment:
    '保存一次已视觉确认的用户地图传送区间。'

    start_at: float
    action_at: float
    end_at: float
    target: str


_KEY_ALIASES = {
    "shift_l": "shift", "shift_r": "shift",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt",
    "return": "enter", "escape": "esc",
}


def normalize_key(value: str) -> str:
    key = str(value).strip().lower()
    if key.startswith("key."):
        key = key[4:]
    return _KEY_ALIASES.get(key, key)


def compile_recording(
        raw_events: Iterable[RawRouteEvent], *,
        coordinate_samples: Iterable[CoordinateSample] = (),
        teleport_segments: Iterable[TeleportSegment] = (),
        stopped_at: float | None = None,
        checkpoint_tolerance: float = 900.0,
        ) -> tuple[RouteEvent, ...]:
    '配对键鼠事件、合并镜头移动并插入稀疏坐标节点。'
    ordered = sorted(raw_events, key=lambda item: (float(item.at), int(item.order)))
    samples = tuple(coordinate_samples)
    ordered, samples, removed_duration = _replace_teleport_segments(
        ordered, samples, tuple(teleport_segments))
    adjusted_stopped_at = (
        max(0.0, float(stopped_at) - removed_duration)
        if stopped_at is not None else 0.0)
    end_time = max(
        [adjusted_stopped_at, *(_raw_event_end(item) for item in ordered)],
        default=0.0,
    )
    compiled: list[tuple[float, int, RouteEvent]] = []
    keys_down: dict[str, tuple[float, int]] = {}
    mouse_down: dict[str, tuple[float, float, float, bool, int]] = {}
    last_pointer = (0.5, 0.5)
    relative_buffer: list[RawRouteEvent] = []

    def append(event: RouteEvent, order: int) -> None:
        compiled.append((event.at, order, event))

    def flush_relative() -> None:
        nonlocal relative_buffer
        if not relative_buffer:
            return
        chunk: list[RawRouteEvent] = []
        chunk_dx = chunk_dy = 0

        def emit(items: list[RawRouteEvent], dx: int, dy: int) -> None:
            if not items or not (dx or dy):
                return
            first = items[0]
            append(RouteEvent(
                float(first.at), "mouse_move_rel", {
                    "dx": dx, "dy": dy,

                    "duration": 0.0,
                }), first.order)

        for item in relative_buffer:
            item_dx = _clip_relative_move(item.data.get("dx", 0))
            item_dy = _clip_relative_move(item.data.get("dy", 0))
            duration_exceeded = bool(
                chunk and _raw_event_end(item) - float(chunk[0].at)
                > RELATIVE_MOUSE_MERGE_WINDOW_S)
            range_exceeded = bool(
                chunk and (abs(chunk_dx + item_dx) > 10_000
                           or abs(chunk_dy + item_dy) > 10_000))
            if duration_exceeded or range_exceeded:
                emit(chunk, chunk_dx, chunk_dy)
                chunk = []
                chunk_dx = chunk_dy = 0
            chunk.append(item)
            chunk_dx += item_dx
            chunk_dy += item_dy
        emit(chunk, chunk_dx, chunk_dy)
        relative_buffer = []

    def append_key_hold(at: float, key: str, duration: float, order: int) -> None:
        remaining = max(0.01, float(duration))
        offset = 0.0
        while remaining > 1e-9:
            part = min(60.0, remaining)
            if 0.0 < remaining - part < 0.01:
                part = remaining - 0.01
            append(RouteEvent(
                at + offset, "key_hold", {"key": key, "duration": part}), order)
            offset += part
            remaining -= part

    def append_mouse_hold(
            at: float, button: str, x: float, y: float,
            duration: float, positioned: bool, order: int) -> None:
        remaining = max(0.01, float(duration))
        offset = 0.0
        while remaining > 1e-9:
            part = min(5.0, remaining)
            if 0.0 < remaining - part < 0.01:
                part = remaining - 0.01
            append(RouteEvent(
                at + offset, "mouse_click", {
                    "button": button,
                    "x": x,
                    "y": y,
                    "duration": part,
                    "positioned": bool(positioned),
                }), order)
            offset += part
            remaining -= part

    for item in ordered:
        kind = item.kind
        if kind != "mouse_move_rel":
            flush_relative()
        if kind == "key_down":
            key = normalize_key(str(item.data.get("key", "")))
            keys_down.setdefault(key, (float(item.at), item.order))
        elif kind == "key_up":
            key = normalize_key(str(item.data.get("key", "")))
            started = keys_down.pop(key, None)
            if started is not None:
                at, order = started
                append_key_hold(at, key, float(item.at) - at, order)
        elif kind == "mouse_move_rel":
            if (relative_buffer and
                    float(item.at) - _raw_event_end(relative_buffer[-1])
                    > RELATIVE_MOUSE_MERGE_WINDOW_S):
                flush_relative()
            relative_buffer.append(item)
        elif kind == "heading_anchor":
            append(RouteEvent(float(item.at), "heading_anchor", {
                "degrees": float(item.data.get("degrees", 0.0)) % 360.0,
                "tolerance": float(item.data.get("tolerance", 7.0)),
            }), item.order)
        elif kind == "mouse_down":
            button = str(item.data.get("button", "left")).lower()
            x = _clip01(item.data.get("x", last_pointer[0]))
            y = _clip01(item.data.get("y", last_pointer[1]))
            positioned = bool(item.data.get(
                "positioned", button == "left"))
            last_pointer = (x, y)
            mouse_down.setdefault(
                button, (float(item.at), x, y, positioned, item.order))
        elif kind == "mouse_up":
            button = str(item.data.get("button", "left")).lower()
            x = _clip01(item.data.get("x", last_pointer[0]))
            y = _clip01(item.data.get("y", last_pointer[1]))
            last_pointer = (x, y)
            started = mouse_down.pop(button, None)
            if started is None:
                continue
            at, x1, y1, positioned, order = started
            duration = max(0.01, float(item.at) - at)
            if (positioned and button == "left"
                    and hypot(x - x1, y - y1) >= 0.008):
                append(RouteEvent(at, "mouse_drag", {
                    "button": button, "x1": x1, "y1": y1,
                    "x2": x, "y2": y, "duration": min(10.0, duration),
                }), order)
            else:
                append_mouse_hold(
                    at, button, x1, y1, duration, positioned, order)
        elif kind == "mouse_wheel":
            append(RouteEvent(float(item.at), "mouse_wheel", {
                "notches": int(item.data.get("notches", 0)),
                "x": _clip01(item.data.get("x", last_pointer[0])),
                "y": _clip01(item.data.get("y", last_pointer[1])),
                "positioned": bool(item.data.get("positioned", True)),
            }), item.order)
        elif kind == "teleport":
            append(RouteEvent(float(item.at), "teleport", {
                "target": str(item.data.get("target") or "").strip(),
            }), item.order)
        elif kind == "gather":
            append(RouteEvent(float(item.at), "key_hold", {
                "key": "f", "duration": 0.05,
            }), item.order)
        elif kind == "snapshot":
            append(RouteEvent(float(item.at), "snapshot", {
                "name": str(item.data.get("name") or "录制标记"),
            }), item.order)

    flush_relative()
    closing_order = max((item.order for item in ordered), default=0) + 1
    for key, (at, order) in keys_down.items():
        append_key_hold(at, key, end_time - at, order)
    for button, (at, x, y, positioned, order) in mouse_down.items():
        append_mouse_hold(
            at, button, x, y, end_time - at, positioned, order)

    compressed_samples = compress_coordinates(samples)
    for index, sample in enumerate(compressed_samples):
        append(RouteEvent(float(sample.at), "checkpoint", {
            "x": float(sample.x), "y": float(sample.y),
            "tolerance": float(checkpoint_tolerance), "retry": 2,
        }), closing_order + index)

    result = tuple(item[2] for item in sorted(compiled, key=lambda row: (row[0], row[1])))
    return result


def _replace_teleport_segments(
        events: list[RawRouteEvent],
        samples: tuple[CoordinateSample, ...],
        segments: tuple[TeleportSegment, ...],
        ) -> tuple[list[RawRouteEvent], tuple[CoordinateSample, ...], float]:
    '仅替换边界安全的已确认传送段，其余段保持原始事件。'
    accepted: list[TeleportSegment] = []
    for segment in sorted(segments, key=lambda item: float(item.start_at)):
        if not _valid_teleport_segment(segment):
            continue
        if accepted and float(segment.start_at) <= float(accepted[-1].end_at):
            continue
        if not _teleport_boundaries_safe(events, segment):
            continue
        accepted.append(segment)
    if not accepted:
        return events, samples, 0.0

    def containing(at: float) -> TeleportSegment | None:
        return next((segment for segment in accepted
                     if float(segment.start_at) <= at <= float(segment.end_at)), None)

    def removed_before(at: float) -> float:
        return sum(
            float(segment.end_at) - float(segment.start_at)
            for segment in accepted if float(segment.end_at) < at)

    rewritten: list[RawRouteEvent] = []
    for event in events:
        at = float(event.at)
        if containing(at) is not None:
            continue
        shift = removed_before(at)
        data = dict(event.data)
        if "end_at" in data:
            try:
                data["end_at"] = max(0.0, float(data["end_at"]) - shift)
            except (TypeError, ValueError, OverflowError):
                data.pop("end_at", None)
        rewritten.append(RawRouteEvent(
            max(0.0, at - shift), event.kind, data, event.order))

    for segment in accepted:
        start = float(segment.start_at)
        shift = removed_before(start)
        inside_orders = [
            event.order for event in events
            if start <= float(event.at) <= float(segment.end_at)]
        order = min(inside_orders, default=0)
        rewritten.append(RawRouteEvent(
            max(0.0, start - shift),
            "teleport",
            {"target": str(segment.target).strip()},
            order,
        ))
    rewritten.sort(key=lambda item: (float(item.at), int(item.order)))

    shifted_samples = []
    for sample in samples:
        at = float(sample.at)
        if containing(at) is not None:
            continue
        shifted_samples.append(CoordinateSample(
            max(0.0, at - removed_before(at)),
            float(sample.x), float(sample.y), str(sample.movement),
        ))
    removed_duration = sum(
        float(segment.end_at) - float(segment.start_at)
        for segment in accepted)
    return rewritten, tuple(shifted_samples), removed_duration


def _valid_teleport_segment(segment: TeleportSegment) -> bool:
    try:
        start = float(segment.start_at)
        action = float(segment.action_at)
        end = float(segment.end_at)
    except (TypeError, ValueError, OverflowError):
        return False
    target = str(segment.target or "").strip()
    return bool(target and 0.0 <= start <= action < end and end - start <= 60.0)


def _teleport_boundaries_safe(
        events: list[RawRouteEvent], segment: TeleportSegment) -> bool:
    '拒绝跨段长按及传送按钮点击后出现的新用户输入。'
    start = float(segment.start_at)
    action = float(segment.action_at)
    end = float(segment.end_at)
    held: dict[tuple[str, str], float] = {}
    for event in events:
        at = float(event.at)
        kind = str(event.kind)
        if kind == "key_down":
            held[("key", normalize_key(str(event.data.get("key", ""))))] = at
        elif kind == "mouse_down":
            held[("mouse", str(event.data.get("button", "")).lower())] = at
        elif kind in {"key_up", "mouse_up"}:
            category = "key" if kind == "key_up" else "mouse"
            value = (normalize_key(str(event.data.get("key", "")))
                     if category == "key"
                     else str(event.data.get("button", "")).lower())
            down = held.pop((category, value), None)
            if down is not None and (
                    down < start < at or down < end < at):
                return False
        if action + 0.15 < at < end and kind in {
                "key_down", "mouse_down", "mouse_wheel", "mouse_move_rel"}:
            return False
    return not any(down < start or start <= down <= end
                   for down in held.values())


def compress_coordinates(
        samples: tuple[CoordinateSample, ...], *,
        minimum_distance: float = 2800.0,
        maximum_gap_s: float = 3.0,
        ) -> tuple[CoordinateSample, ...]:
    '把连续定位样本压成适合回放纠偏的稀疏节点。'
    if not samples:
        return ()
    ordered = tuple(sorted(samples, key=lambda item: float(item.at)))
    jitter_distance = max(25.0, float(minimum_distance) * 0.02)
    periodic_distance = max(jitter_distance, float(minimum_distance) * 0.25)
    selected = [ordered[0]]
    for index, sample in enumerate(ordered[1:-1], start=1):
        previous = selected[-1]
        distance = hypot(sample.x - previous.x, sample.y - previous.y)
        if distance < jitter_distance:
            continue
        mode_changed = sample.movement != previous.movement
        gap = float(sample.at) - float(previous.at)
        periodic = gap >= maximum_gap_s and distance >= periodic_distance
        if (distance >= minimum_distance or periodic
                or mode_changed
                or _is_sharp_turn(ordered, index, minimum_distance)):
            selected.append(sample)
    if len(ordered) > 1:
        final = ordered[-1]
        final_distance = hypot(
            final.x - selected[-1].x, final.y - selected[-1].y)
        if final is not selected[-1] and final_distance >= jitter_distance:
            selected.append(final)
    return tuple(selected)


def _is_sharp_turn(
        samples: tuple[CoordinateSample, ...], index: int,
        minimum_distance: float) -> bool:
    '检测应保留的明显转向节点。'
    current = samples[index]
    adjacent_before = samples[index - 1]
    adjacent_after = samples[index + 1]
    if (hypot(current.x - adjacent_before.x, current.y - adjacent_before.y) < 1e-6
            or hypot(adjacent_after.x - current.x,
                     adjacent_after.y - current.y) < 1e-6):
        return False
    radius = max(100.0, float(minimum_distance) * 0.25)
    before = adjacent_before
    for position in range(index - 1, max(-1, index - 65), -1):
        before = samples[position]
        if hypot(current.x - before.x, current.y - before.y) >= radius:
            break
    else:
        return False
    after = adjacent_after
    for position in range(index + 1, min(len(samples), index + 65)):
        after = samples[position]
        if hypot(after.x - current.x, after.y - current.y) >= radius:
            break
    else:
        return False
    ax, ay = current.x - before.x, current.y - before.y
    bx, by = after.x - current.x, after.y - current.y
    length_a = hypot(ax, ay)
    length_b = hypot(bx, by)
    if length_a < 1e-6 or length_b < 1e-6:
        return False
    cosine = (ax * bx + ay * by) / (length_a * length_b)
    return cosine < 0.80


def _clip_relative_move(value: Any) -> int:
    '把异常镜头增量限制在单事件安全范围内。'
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(-10_000, min(10_000, number))


def _clip01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    return max(0.0, min(1.0, number))


def _raw_event_end(event: RawRouteEvent) -> float:
    '返回原始事件结束时间；在线合并的镜头移动会携带endat。'
    try:
        return max(float(event.at), float(event.data.get("end_at", event.at)))
    except (TypeError, ValueError, OverflowError):
        return float(event.at)
