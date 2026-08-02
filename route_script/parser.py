'解析和生成用户可修改的路线TXT。'
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from math import isfinite
from pathlib import Path
from typing import Any

from .model import (
    SCHEMA_VERSION,
    RouteEvent,
    RouteMetadata,
    RouteScript,
    RouteValidationError,
)


_BOOL_TRUE = {"是", "开", "true", "1", "yes", "on"}
_BOOL_FALSE = {"否", "关", "false", "0", "no", "off"}
_ACTION_NAMES = {
    "按键": "key_hold",
    "点击": "mouse_click",
    "拖动": "mouse_drag",
    "镜头移动": "mouse_move_rel",
    "方向锚点": "heading_anchor",
    "滚轮": "mouse_wheel",
    "坐标": "checkpoint",
    "视觉检查": "visual_checkpoint",

    "采集": "legacy_gather",
    "图谱传送": "teleport",
    "截图": "snapshot",
}
_HEADER_NAMES = {
    "名称": "name",
    "起始传送点": "start_teleport",
    "录制宽度": "client_width",
    "录制高度": "client_height",
    "DPI": "dpi_scale",
    "地图记录": "coordinate_correction",
    "坐标纠偏": "coordinate_correction",

    "自动采集": "legacy_auto_gather",
    "创建时间": "created_at",
    "路线版本": "schema_version",
}


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_fields(line: str) -> list[str]:
    return [part.strip() for part in line.split("|")]


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    lowered = value.lower()
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        return value


def _parse_options(parts: list[str], line_number: int) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        if "=" not in part:
            raise RouteValidationError(f"参数必须使用 名称=值：{part}", line_number)
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            raise RouteValidationError("参数名称不能为空", line_number)
        if name in options:
            raise RouteValidationError(f"参数重复：{name}", line_number)
        options[name] = _parse_value(value)
    return options


def parse_text(text: str, default_name: str = "") -> RouteScript:
    text = str(text).lstrip("\ufeff")
    headers: dict[str, Any] = {
        "name": default_name,
        "schema_version": SCHEMA_VERSION,
    }
    header_lines: dict[str, int] = {}
    events: list[RouteEvent] = []
    cursor = 0.0
    in_events = False
    trailing_wait = 0.0

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = re.split(r"\s+#", raw_line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line == "[事件]":
            if in_events:
                raise RouteValidationError("[事件]段不能重复", line_number)
            in_events = True
            continue
        if not in_events and "=" in line:
            name, value = (part.strip() for part in line.split("=", 1))
            field = _HEADER_NAMES.get(name)
            if field is None:
                raise RouteValidationError(f"未知文件头 {name!r}", line_number)
            if field in header_lines:
                raise RouteValidationError(f"文件头重复：{name}", line_number)

            headers[field] = value.strip()
            header_lines[field] = line_number
            continue
        if not in_events:
            raise RouteValidationError("动作必须写在[事件]之后", line_number)

        parts = _split_fields(line)
        command = parts[0]
        if command == "等待":
            if len(parts) != 2:
                raise RouteValidationError("等待格式为：等待 | 秒数", line_number)
            try:
                delay = float(parts[1])
            except ValueError as exc:
                raise RouteValidationError("等待秒数无效", line_number) from exc
            if not 0.0 <= delay <= 3600.0:
                raise RouteValidationError("单次等待必须在0到3600秒之间", line_number)
            cursor += delay
            trailing_wait += delay
            continue

        action = _ACTION_NAMES.get(command)
        if action is None:
            raise RouteValidationError(f"未知操作 {command!r}", line_number)
        args = _parse_action(action, parts[1:], line_number)
        if action == "legacy_gather":
            action = "key_hold"
        events.append(RouteEvent(cursor, action, args, line_number))
        trailing_wait = 0.0

    if trailing_wait > 0.0:
        events.append(RouteEvent(cursor, "wait", {}, 0))

    client_width = _header_int(
        headers, header_lines, "client_width", "录制宽度", 0)
    client_height = _header_int(
        headers, header_lines, "client_height", "录制高度", 0)
    dpi_scale = _header_float(headers, header_lines, "dpi_scale", "DPI", 1.0)
    schema_version = _header_int(
        headers, header_lines, "schema_version", "路线版本", SCHEMA_VERSION)
    coordinate_correction = _header_bool(
        headers, header_lines, "coordinate_correction", "地图记录", False)
    if client_width < 0:
        raise RouteValidationError(
            "录制宽度不能为负数", header_lines.get("client_width"))
    if client_height < 0:
        raise RouteValidationError(
            "录制高度不能为负数", header_lines.get("client_height"))
    if not isfinite(dpi_scale) or not 0.5 <= dpi_scale <= 4.0:
        raise RouteValidationError(
            "DPI缩放必须在0.5到4.0之间", header_lines.get("dpi_scale"))
    if schema_version != SCHEMA_VERSION:
        raise RouteValidationError(
            f"不支持的路线版本 {schema_version}，当前版本为 {SCHEMA_VERSION}",
            header_lines.get("schema_version"),
        )

    metadata = RouteMetadata(
        name=str(headers.get("name") or default_name).strip(),
        start_teleport=str(headers.get("start_teleport") or "").strip(),
        client_width=client_width,
        client_height=client_height,
        dpi_scale=dpi_scale,
        coordinate_correction=coordinate_correction,
        created_at=str(headers.get("created_at") or ""),
        schema_version=schema_version,
    )
    route = RouteScript(metadata, tuple(events), source_hash(text))
    route.validate()
    return route


def _header_int(
        headers: dict[str, Any], lines: dict[str, int], field: str,
        label: str, default: int) -> int:
    value = headers.get(field, default)
    if isinstance(value, bool):
        raise RouteValidationError(f"{label}必须是整数", lines.get(field))
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RouteValidationError(f"{label}必须是整数", lines.get(field)) from exc
    if isinstance(value, float) and not value.is_integer():
        raise RouteValidationError(f"{label}必须是整数", lines.get(field))
    return number


def _header_float(
        headers: dict[str, Any], lines: dict[str, int], field: str,
        label: str, default: float) -> float:
    value = headers.get(field, default)
    if isinstance(value, bool):
        raise RouteValidationError(f"{label}必须是数字", lines.get(field))
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RouteValidationError(f"{label}必须是数字", lines.get(field)) from exc


def _header_bool(
        headers: dict[str, Any], lines: dict[str, int], field: str,
        label: str, default: bool) -> bool:
    value = headers.get(field, default)
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise RouteValidationError(
        f"{label}只接受 是/否、开/关 或 true/false", lines.get(field))


def _parse_action(action: str, parts: list[str], line: int) -> dict[str, Any]:
    if action == "key_hold":
        if not parts:
            raise RouteValidationError("按键格式为：按键 | 键名 | 持续=秒数", line)
        return {"key": parts[0].lower(), **_action_options(
            parts[1:], line, {"持续": "duration"})}
    if action == "mouse_click":
        if not parts:
            raise RouteValidationError("点击缺少鼠标按键", line)
        return {"button": parts[0].lower(), **_action_options(
            parts[1:], line,
            {
                "横坐标": "x", "纵坐标": "y",
                "持续": "duration", "定位": "positioned",
            })}
    if action == "mouse_drag":
        if not parts:
            raise RouteValidationError("拖动缺少鼠标按键", line)
        return {"button": parts[0].lower(), **_action_options(
            parts[1:], line, {
                "起点横坐标": "x1", "起点纵坐标": "y1",
                "终点横坐标": "x2", "终点纵坐标": "y2", "持续": "duration",
            })}
    if action == "mouse_move_rel":
        return _action_options(parts, line, {
            "横向": "dx", "纵向": "dy", "持续": "duration"})
    if action == "heading_anchor":
        return _action_options(parts, line, {
            "角度": "degrees", "误差": "tolerance"})
    if action == "mouse_wheel":
        return _action_options(parts, line, {
            "格数": "notches", "横坐标": "x", "纵坐标": "y",
            "定位": "positioned",
        })
    if action == "checkpoint":
        return _action_options(parts, line, {
            "横坐标": "x", "纵坐标": "y", "误差": "tolerance", "重试": "retry"})
    if action == "visual_checkpoint":
        return _action_options(parts, line, {
            "文件": "asset", "最大偏移": "max_offset", "重试": "retry"})
    if action == "legacy_gather":

        _action_options(parts, line, {"持续": "duration", "重试": "retry"})
        return {"key": "f", "duration": 0.05}
    if action == "teleport":
        if len(parts) != 1 or not parts[0]:
            raise RouteValidationError("图谱传送格式为：图谱传送 | 目标名称", line)
        return {"target": parts[0]}
    if action == "snapshot":
        if len(parts) != 1 or not parts[0]:
            raise RouteValidationError("截图格式为：截图 | 名称", line)
        return {"name": parts[0]}
    raise RouteValidationError(f"未实现的操作 {action}", line)


def _rename(options: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {names.get(key, key): value for key, value in options.items()}


def _action_options(
        parts: list[str], line: int, names: dict[str, str]) -> dict[str, Any]:
    options = _parse_options(parts, line)
    unknown = [name for name in options if name not in names]
    if unknown:
        raise RouteValidationError(f"未知参数：{unknown[0]}", line)
    return _rename(options, names)


def parse_file(path: Path | str) -> RouteScript:
    file = Path(path)
    return parse_text(file.read_text(encoding="utf-8"), file.stem)


def route_from_dict(raw: dict[str, Any]) -> RouteScript:
    metadata_raw = dict(raw.get("metadata") or {})
    metadata_raw.pop("auto_gather", None)
    if "schema_version" not in metadata_raw:
        metadata_raw["schema_version"] = (
            raw["schema_version"] if "schema_version" in raw else SCHEMA_VERSION)
    metadata = RouteMetadata(**metadata_raw)
    events = tuple(_event_from_dict(item) for item in raw.get("events") or ())
    route = RouteScript(metadata, events, str(raw.get("source_hash") or ""))
    route.validate()
    return route


def _event_from_dict(item: dict[str, Any]) -> RouteEvent:
    '将旧版视觉采集节点迁移为一次普通F交互。'
    action = str(item["action"])
    args = dict(item.get("args") or {})
    if action == "gather":
        action = "key_hold"
        args = {"key": "f", "duration": 0.05}
    return RouteEvent(
        at=float(item["at"]),
        action=action,
        args=args,
        source_line=int(item.get("source_line") or 0),
    )


def load_json(path: Path | str) -> RouteScript:
    return route_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def format_text(route: RouteScript) -> str:
    route.validate()
    metadata = route.metadata
    lines = [
        "# HOKWorld 自定义路线。通常由录制器生成，可手动修改。",
        f"路线版本 = {metadata.schema_version}",
        f"名称 = {metadata.name}",
        f"起始传送点 = {metadata.start_teleport}",
        f"录制宽度 = {metadata.client_width}",
        f"录制高度 = {metadata.client_height}",
        f"DPI = {metadata.dpi_scale:g}",
        f"地图记录 = {'是' if metadata.coordinate_correction else '否'}",
        f"创建时间 = {metadata.created_at}",
        "",
        "[事件]",
    ]
    cursor = 0.0
    for event in route.events:
        wait = max(0.0, float(event.at) - cursor)
        if wait > 0.0005:
            lines.append(f"等待 | {wait:.4f}")
            cursor += wait
        formatted = _format_event(event)
        if formatted:
            lines.append(formatted)
    return "\n".join(lines).rstrip() + "\n"


def _format_event(event: RouteEvent) -> str:
    a = event.args
    if event.action == "wait":
        return ""
    if event.action == "key_hold":
        return f"按键 | {a['key']} | 持续={float(a['duration']):.4f}"
    if event.action == "mouse_click":
        positioned = bool(a.get(
            "positioned", str(a.get("button", "")).lower() == "left"))
        return (f"点击 | {a['button']} | 横坐标={float(a['x']):.6f} | "
                f"纵坐标={float(a['y']):.6f} | 持续={float(a['duration']):.4f} | "
                f"定位={'是' if positioned else '否'}")
    if event.action == "mouse_drag":
        return (f"拖动 | {a['button']} | 起点横坐标={float(a['x1']):.6f} | "
                f"起点纵坐标={float(a['y1']):.6f} | 终点横坐标={float(a['x2']):.6f} | "
                f"终点纵坐标={float(a['y2']):.6f} | 持续={float(a['duration']):.4f}")
    if event.action == "mouse_move_rel":
        return (f"镜头移动 | 横向={int(a['dx'])} | 纵向={int(a['dy'])} | "
                f"持续={float(a.get('duration', 0.0)):.4f}")
    if event.action == "heading_anchor":
        return (f"方向锚点 | 角度={float(a['degrees']):.2f} | "
                f"误差={float(a.get('tolerance', 7.0)):.1f}")
    if event.action == "mouse_wheel":
        positioned = bool(a.get("positioned", True))
        return (f"滚轮 | 格数={int(a['notches'])} | 横坐标={float(a['x']):.6f} | "
                f"纵坐标={float(a['y']):.6f} | "
                f"定位={'是' if positioned else '否'}")
    if event.action == "checkpoint":
        return (f"坐标 | 横坐标={float(a['x']):.3f} | 纵坐标={float(a['y']):.3f} | "
                f"误差={float(a['tolerance']):.1f} | 重试={int(a.get('retry', 2))}")
    if event.action == "visual_checkpoint":
        return (f"视觉检查 | 文件={a['asset']} | "
                f"最大偏移={float(a.get('max_offset', 14.0)):.1f} | "
                f"重试={int(a.get('retry', 2))}")
    if event.action == "teleport":
        return f"图谱传送 | {a['target']}"
    if event.action == "snapshot":
        return f"截图 | {a['name']}"
    raise RouteValidationError(f"无法输出操作 {event.action}")


def with_source_hash(route: RouteScript, text: str) -> RouteScript:
    return replace(route, source_hash=source_hash(text))
