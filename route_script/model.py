'定义自定义路线的数据结构和安全约束。'
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any


SCHEMA_VERSION = 1
SUPPORTED_ACTIONS = frozenset({
    "wait",
    "key_hold",
    "mouse_click",
    "mouse_drag",
    "mouse_move_rel",
    "heading_anchor",
    "mouse_wheel",
    "checkpoint",
    "visual_checkpoint",
    "teleport",
    "snapshot",
})
FUNCTION_KEYS = frozenset(f"f{number}" for number in range(1, 13))
ROUTE_CONTROL_KEYS = frozenset({"f10", "f11", "f12"})
SUPPORTED_KEYS = frozenset({
    *tuple(chr(code) for code in range(ord("a"), ord("z") + 1)),
    *tuple(str(number) for number in range(10)),
    *(FUNCTION_KEYS - ROUTE_CONTROL_KEYS),
    "tab", "esc", "space", "shift", "ctrl", "alt", "enter",
    "up", "down", "left", "right",
})
SUPPORTED_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})
_INVALID_ROUTE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def normalize_route_name(name: str) -> str:
    '验证并标准化可安全用作Windows文件名和目录名的路线名称。'
    raw = str(name).strip()
    if _INVALID_ROUTE_NAME.search(raw):
        raise RouteValidationError("路线名称包含Windows不允许的字符")
    value = raw.strip(" .")
    if not value:
        raise RouteValidationError("路线名称不能为空")
    if len(value) > 80:
        raise RouteValidationError("路线名称不能超过80字")
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise RouteValidationError(f"Windows保留名称不能作为路线名：{value}")
    if value != raw:
        raise RouteValidationError("路线名称不能以空格或句点结尾")
    return value


class RouteValidationError(ValueError):
    '路线格式或参数不安全。'

    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        prefix = f"第{line}行：" if line else ""
        super().__init__(prefix + message)


@dataclass(frozen=True)
class RouteMetadata:
    '保存路线文件头和录制环境。'

    name: str
    start_teleport: str = ""
    client_width: int = 0
    client_height: int = 0
    dpi_scale: float = 1.0
    coordinate_correction: bool = False
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        normalize_route_name(self.name)
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise RouteValidationError("路线版本必须是整数")
        if self.schema_version != SCHEMA_VERSION:
            raise RouteValidationError(
                f"不支持的路线版本 {self.schema_version}，当前版本为 {SCHEMA_VERSION}")
        if (isinstance(self.client_width, bool) or isinstance(self.client_height, bool)
                or not isinstance(self.client_width, int)
                or not isinstance(self.client_height, int)):
            raise RouteValidationError("录制分辨率必须是整数")
        if self.client_width < 0 or self.client_height < 0:
            raise RouteValidationError("录制分辨率不能为负数")
        if isinstance(self.dpi_scale, bool):
            raise RouteValidationError("DPI缩放必须是数字")
        try:
            dpi_scale = float(self.dpi_scale)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RouteValidationError("DPI缩放必须是数字") from exc
        if not isfinite(dpi_scale) or not 0.5 <= dpi_scale <= 4.0:
            raise RouteValidationError("DPI缩放必须在0.5到4.0之间")
        if not isinstance(self.coordinate_correction, bool):
            raise RouteValidationError("地图记录必须是布尔值")


@dataclass(frozen=True)
class RouteEvent:
    '保存一个已编译的时间轴动作。'

    at: float
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    source_line: int = 0

    def validate(self) -> None:
        if self.action not in SUPPORTED_ACTIONS:
            raise RouteValidationError(
                f"未知操作 {self.action!r}", self.source_line or None)
        if not isfinite(float(self.at)) or self.at < 0.0:
            raise RouteValidationError("事件时间必须为非负数", self.source_line or None)

        validator = getattr(self, f"_validate_{self.action}")
        validator()

    def _number(self, name: str, minimum: float, maximum: float) -> float:
        raw = self.args.get(name)
        if isinstance(raw, bool):
            raise RouteValidationError(
                f"{self.action} 缺少有效参数 {name}", self.source_line or None)
        try:
            value = float(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteValidationError(
                f"{self.action} 缺少有效参数 {name}", self.source_line or None) from exc
        if not isfinite(value) or not minimum <= value <= maximum:
            raise RouteValidationError(
                f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间",
                self.source_line or None,
            )
        return value

    def _integer(self, name: str, minimum: int, maximum: int) -> int:
        raw = self.args.get(name)
        if isinstance(raw, bool):
            raise RouteValidationError(
                f"{name}必须是整数", self.source_line or None)
        try:
            if isinstance(raw, int):
                value = raw
            else:
                text = str(raw).strip()
                if not text or not re.fullmatch(r"[+-]?\d+", text):
                    raise ValueError
                value = int(text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RouteValidationError(
                f"{name}必须是整数", self.source_line or None) from exc
        if not minimum <= value <= maximum:
            raise RouteValidationError(
                f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间",
                self.source_line or None,
            )
        return value

    def _boolean(self, name: str, default: bool) -> bool:
        raw = self.args.get(name, default)
        if not isinstance(raw, bool):
            raise RouteValidationError(
                f"{name}必须是布尔值", self.source_line or None)
        return raw

    def _validate_key_hold(self) -> None:
        key = str(self.args.get("key", "")).lower()
        if key not in SUPPORTED_KEYS:
            raise RouteValidationError(f"不支持的按键 {key!r}", self.source_line or None)
        self._number("duration", 0.01, 60.0)

    def _validate_wait(self) -> None:
        '等待只作为时间轴尾部标记，不需要额外参数。'

    def _validate_mouse_click(self) -> None:
        if str(self.args.get("button", "")) not in SUPPORTED_MOUSE_BUTTONS:
            raise RouteValidationError("不支持的鼠标按键", self.source_line or None)
        self._number("x", 0.0, 1.0)
        self._number("y", 0.0, 1.0)
        self._number("duration", 0.01, 5.0)
        self._boolean(
            "positioned",
            str(self.args.get("button", "")).lower() == "left",
        )

    def _validate_mouse_drag(self) -> None:
        if str(self.args.get("button", "")) != "left":
            raise RouteValidationError("当前只支持鼠标左键拖动", self.source_line or None)
        for name in ("x1", "y1", "x2", "y2"):
            self._number(name, 0.0, 1.0)
        self._number("duration", 0.01, 10.0)

    def _validate_mouse_move_rel(self) -> None:
        self._integer("dx", -10000, 10000)
        self._integer("dy", -10000, 10000)
        self._number("duration", 0.0, 5.0)

    def _validate_heading_anchor(self) -> None:
        self._number("degrees", 0.0, 360.0)
        self._number("tolerance", 1.0, 45.0)

    def _validate_mouse_wheel(self) -> None:
        self._integer("notches", -100, 100)
        self._number("x", 0.0, 1.0)
        self._number("y", 0.0, 1.0)
        self._boolean("positioned", True)

    def _validate_checkpoint(self) -> None:
        self._number("x", -1_000_000.0, 1_000_000.0)
        self._number("y", -1_000_000.0, 1_000_000.0)
        self._number("tolerance", 50.0, 50_000.0)
        retry = self._retry("坐标")
        if not 0 <= retry <= 5:
            raise RouteValidationError("坐标重试次数必须在0到5之间", self.source_line or None)

    def _validate_visual_checkpoint(self) -> None:
        asset = str(self.args.get("asset", "")).replace("\\", "/").strip("/")
        if (not re.fullmatch(r"anchors/[A-Za-z0-9_.-]+\.npz", asset)
                or ".." in asset):
            raise RouteValidationError(
                "视觉检查文件必须位于 anchors 目录", self.source_line or None)
        self._number("max_offset", 2.0, 80.0)
        retry = self._retry("视觉检查")
        if not 0 <= retry <= 5:
            raise RouteValidationError(
                "视觉检查重试次数必须在0到5之间", self.source_line or None)

    def _validate_teleport(self) -> None:
        target = str(self.args.get("target", "")).strip()
        if (not target or len(target) > 80
                or any(char in target for char in "|\r\n")):
            raise RouteValidationError(
                "图谱传送目标不能为空、不能超过80字或包含分隔符",
                self.source_line or None,
            )

    def _retry(self, label: str) -> int:
        value = self.args.get("retry", 2)
        if isinstance(value, bool):
            raise RouteValidationError(
                f"{label}重试次数必须是整数", self.source_line or None)
        try:
            if isinstance(value, int):
                return value
            text = str(value).strip()
            if not text or not re.fullmatch(r"[+-]?\d+", text):
                raise ValueError
            return int(text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RouteValidationError(
                f"{label}重试次数必须是整数", self.source_line or None) from exc

    def _validate_snapshot(self) -> None:
        name = str(self.args.get("name", "")).strip()
        if not name or len(name) > 80:
            raise RouteValidationError("截图名称不能为空且不能超过80字", self.source_line or None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RouteScript:
    '保存一条可执行路线。'

    metadata: RouteMetadata
    events: tuple[RouteEvent, ...]
    source_hash: str = ""

    def validate(self) -> None:
        self.metadata.validate()
        if not self.events:
            raise RouteValidationError("路线至少需要一个动作")
        if len(self.events) > 100_000:
            raise RouteValidationError("路线动作超过100000条")
        last_at = -1.0
        active_until: dict[str, float] = {}
        mouse_active_until: dict[str, float] = {}
        for event in self.events:
            event.validate()
            if event.at < last_at:
                raise RouteValidationError(
                    "事件时间必须按顺序递增", event.source_line or None)
            last_at = event.at
            if event.action == "key_hold":
                key = str(event.args["key"]).lower()
                if active_until.get(key, -1.0) > event.at + 1e-6:
                    raise RouteValidationError(
                        f"按键 {key} 的按住区间发生重叠", event.source_line or None)
                active_until[key] = event.at + float(event.args["duration"])
            elif event.action == "mouse_click":
                button = str(event.args["button"]).lower()
                if mouse_active_until.get(button, -1.0) > event.at + 1e-6:
                    raise RouteValidationError(
                        f"鼠标键 {button} 的按住区间发生重叠",
                        event.source_line or None,
                    )
                mouse_active_until[button] = (
                    event.at + float(event.args["duration"]))
        if last_at > 24 * 60 * 60:
            raise RouteValidationError("单条路线不能超过24小时")

    @property
    def duration(self) -> float:
        end = 0.0
        for event in self.events:
            end = max(end, event.at)
            if event.action == "key_hold":
                end = max(end, event.at + float(event.args.get("duration", 0.0)))
            elif event.action == "mouse_click":
                end = max(end, event.at + float(event.args.get("duration", 0.0)))
        return end

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_hash": self.source_hash,
            "metadata": asdict(self.metadata),
            "events": [event.to_dict() for event in self.events],
        }
