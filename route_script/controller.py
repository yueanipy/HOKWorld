'自定义路线界面的文件、配置与交互控制器。'
from __future__ import annotations

import json
import os
from functools import cmp_to_key
from pathlib import Path

from PySide6.QtCore import QCollator, QLocale, Qt, Signal
from qfluentwidgets import MessageBox

from config import cfg

from .store import RouteStore
from app_ui.route_script import (
    RouteNameDialog,
    RouteQuickRunCardView,
    RouteScriptCardView,
    RouteScriptInterfaceView,
)


class _ValidatedRouteNameDialog(RouteNameDialog):
    '在控制层校验路线名称，视图只负责显示错误。'

    def validate(self) -> bool:
        try:
            self.route_name = RouteStore.normalize_name(self.name_edit.text())
        except (TypeError, ValueError) as exc:
            self.show_error(str(exc))
            return False
        return True


class RouteScriptCard(RouteScriptCardView):
    '路线菜单中的录制、回放和文件管理工作台。'

    playRequested = Signal(str, int, bool)
    stopRequested = Signal()
    recordRequested = Signal(str, str, bool)
    finishRecordRequested = Signal()
    catalogChanged = Signal(str)
    selectionChanged = Signal(str)
    message = Signal(str)

    _DESC = "F11开始自动脚本，F12停止录制或自动脚本"

    def __init__(self, parent=None, *, store: RouteStore | None = None) -> None:
        super().__init__(parent)
        self.store = store or RouteStore()
        self._mode = "idle"
        self._invalid_routes: dict[str, str] = {}
        self._initialize_seed_once()
        self.teleport_combo.set_search_items(
            ("", *self._load_teleport_names()))
        self.loop_spin.setValue(max(1, int(cfg.get("route_loop_count") or 1)))
        self.coordinate_switch.setChecked(
            bool(cfg.get("route_coordinate_correction")))

        self.play_btn.clicked.connect(self._request_play)
        self.stop_btn.clicked.connect(self._request_stop)
        self.record_btn.clicked.connect(self._request_record)
        self.refresh_btn.clicked.connect(lambda: self.refresh_routes(announce=True))
        self.edit_btn.clicked.connect(self._edit_selected)
        self.new_btn.clicked.connect(self._create_route)
        self.copy_btn.clicked.connect(self._copy_selected)
        self.rename_btn.clicked.connect(self._rename_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.folder_btn.clicked.connect(self._open_folder)
        self.route_combo.currentTextChanged.connect(self._on_selection_changed)
        self.loop_spin.valueChanged.connect(
            lambda value: cfg.set("route_loop_count", int(value)))
        self.coordinate_switch.checkedChanged.connect(
            lambda value: cfg.set("route_coordinate_correction", bool(value)))
        self.refresh_routes(announce=False)

    @property
    def mode(self) -> str:
        return self._mode

    def selected_name(self) -> str:
        return str(self.route_combo.currentText() or "").strip()

    def select_route(self, name: str) -> None:
        '无递归信号地同步另一个嵌入入口的当前路线。'
        text = str(name or "").strip()
        if not text or text == self.selected_name():
            return
        index = self.route_combo.findText(text)
        if index < 0:
            return
        self.route_combo.blockSignals(True)
        self.route_combo.setCurrentIndex(index)
        self.route_combo.blockSignals(False)
        self._on_selection_changed(text)

    def start_teleport(self) -> str:
        return str(self.teleport_combo.currentText() or "").strip()

    def _set_start_teleport(self, value: str) -> None:
        '回显预设列表内或用户自行填写的传送点。'
        text = str(value or "").strip()
        index = self.teleport_combo.findText(text)
        if index >= 0:
            self.teleport_combo.setCurrentIndex(index)
        else:


            self.teleport_combo.setText(text)

    def load_selected(self):
        name = self.selected_name()
        if not name:
            raise FileNotFoundError("没有可执行的路线，请先录制或新建路线")
        return self.store.load(name)

    def refresh_routes(self, *, select: str = "", announce: bool = False) -> None:
        '扫描TXT并同步JSON；运行和录制过程中禁止刷新。'
        if self._mode != "idle":
            if announce:
                self.message.emit("路线运行或录制中，暂不能刷新")
            return
        preferred = str(select or self.selected_name()
                        or cfg.get("route_script_file") or "")
        _routes, errors = self.store.sync_all()
        names = self.store.list_names()
        self._invalid_routes = dict(errors)
        self.route_combo.blockSignals(True)
        self.route_combo.clear()
        self.route_combo.addItems(list(names))
        if preferred in names:
            self.route_combo.setCurrentText(preferred)
        elif names:
            self.route_combo.setCurrentText(names[0])
        self.route_combo.blockSignals(False)
        self._on_selection_changed(self.selected_name())
        self._update_controls()
        if announce:
            if errors:
                self.message.emit(
                    f"刷新完成：{len(names)}条路线，{len(errors)}条格式无效")
            else:
                self.message.emit(f"刷新完成：已同步{len(names)}条路线")
        self.catalogChanged.emit(self.selected_name())

    def set_playing(self) -> None:
        self._mode = "playing"
        self.record_btn.setText("开始录制")
        self._set_content("运行中…")
        self._update_controls()

    def set_paused(self, paused: bool) -> None:
        if self._mode not in {"playing", "paused"}:
            return
        self._mode = "paused" if paused else "playing"
        self._set_content("已暂停" if paused else "运行中…")
        self._update_controls()

    def set_recording(self) -> None:
        self._mode = "recording"
        self.record_btn.setText("完成录制")
        self._set_content("录制中 · 按F12结束并保存")
        self._update_controls()

    def set_stopping(self, text: str = "停止中…") -> None:
        self._mode = "stopping"
        self._set_content(text)
        self._update_controls()

    def reset_runtime(self, text: str = "") -> None:
        self._mode = "idle"
        self.record_btn.setText("开始录制")
        self._set_content(text or self._DESC)
        self._update_controls()

    def set_runtime_state(self, text: str) -> None:
        if self._mode != "idle" and text:
            self._set_content(str(text))

    def mark_invalid(self, name: str, error: str) -> None:
        '记录启动前发现的新解析错误，并立即禁用当前路线。'
        self._invalid_routes[str(name)] = str(error)
        if self.selected_name() == str(name):
            self._set_content(f"配置无效：{error}")
            self._update_controls()

    def set_progress(self, current: int, total: int) -> None:
        if self._mode in {"playing", "paused"} and total > 0:
            prefix = "已暂停" if self._mode == "paused" else "运行中"
            self._set_content(f"{prefix} · {int(current)}/{int(total)}")

    def shutdown(self) -> None:
        '关闭窗口前禁止卡片继续发起文件操作。'
        self._mode = "stopping"
        self.setEnabled(False)

    def _initialize_seed_once(self) -> None:
        if bool(cfg.get("route_seed_initialized")):
            return
        seed_dir = Path(__file__).resolve().parents[1] / "assets" / "route_scripts"
        try:
            for seed in sorted(seed_dir.glob("*.txt")):
                self.store.import_seed(seed)
        except Exception as exc:
            self.message.emit(f"初始化示例路线失败：{exc}")
            return
        cfg.set("route_seed_initialized", True)

    def _set_content(self, text: str) -> None:
        '卡片副标题保持热键说明，运行状态交给页面底部日志。'

    def _update_controls(self) -> None:
        idle = self._mode == "idle"
        selected_name = self.selected_name()
        selected = bool(selected_name)
        selected_valid = selected and selected_name not in self._invalid_routes
        playing = self._mode in {"playing", "paused"}
        recording = self._mode == "recording"
        self.play_btn.setEnabled(idle and selected_valid)
        self.stop_btn.setEnabled(playing or recording)
        self.record_btn.setEnabled(idle or recording)
        for widget in (
                self.route_combo, self.refresh_btn, self.edit_btn, self.new_btn,
                self.copy_btn, self.rename_btn, self.delete_btn, self.folder_btn,
                self.teleport_combo, self.loop_spin,
                self.coordinate_switch):
            widget.setEnabled(idle)
        self.edit_btn.setEnabled(idle and selected)
        self.copy_btn.setEnabled(idle and selected)
        self.rename_btn.setEnabled(idle and selected)
        self.delete_btn.setEnabled(idle and selected)

    def _on_selection_changed(self, name: str) -> None:
        if name:
            cfg.set("route_script_file", str(name))
        self._update_controls()
        error = self._invalid_routes.get(str(name))
        if error and self._mode == "idle":
            self._set_content(f"配置无效：{error}")
        elif self._mode == "idle":
            self._set_content(self._DESC)
        if name and not error and self._mode == "idle":
            try:
                route = self.store.load(str(name))
                self._set_start_teleport(route.metadata.start_teleport)
            except Exception:
                pass
        self.selectionChanged.emit(str(name or ""))

    def _request_play(self) -> None:
        if self._mode != "idle":
            return
        name = self.selected_name()
        if not name:
            self.message.emit("没有可执行的路线")
            return
        self.playRequested.emit(
            name,
            int(self.loop_spin.value()),
            self.coordinate_switch.isChecked(),
        )

    def _request_stop(self) -> None:
        if self._mode in {"playing", "paused", "recording"}:
            self.stopRequested.emit()

    def _request_record(self) -> None:
        if self._mode == "recording":
            self.finishRecordRequested.emit()
            return
        if self._mode != "idle":
            return
        default = self.selected_name() or "新路线"
        name = self._ask_name("录制路线", default)
        if not name:
            return
        existing = next(
            (known for known in self.store.list_names()
             if known.casefold() == name.casefold()),
            "",
        )
        if existing:
            box = MessageBox("覆盖路线", "确认覆盖当前路线？", self.window())
            box.yesButton.setText("确认覆盖")
            box.cancelButton.setText("取消")
            if not box.exec():
                return

            name = existing
        self.recordRequested.emit(
            name,
            self.start_teleport(),
            self.coordinate_switch.isChecked(),
        )

    def request_record_from_hotkey(self) -> None:
        '使用当前选中的路线直接开始录制。'
        if self._mode != "idle":
            return
        name = self.selected_name()
        if not name:
            self.message.emit("请先新建或选择一条路线")
            return
        self.recordRequested.emit(
            name,
            self.start_teleport(),
            self.coordinate_switch.isChecked(),
        )

    def request_play_from_hotkey(self) -> None:
        '使用当前路线响应路线页面的F11启动请求。'
        self._request_play()

    def _ask_name(self, title: str, initial: str = "") -> str:
        dialog = _ValidatedRouteNameDialog(title, initial, self.window())
        return dialog.route_name if dialog.exec() else ""

    def _create_route(self) -> None:
        name = self._ask_name("新建路线", "新路线")
        if not name:
            return
        try:
            path = self.store.create(name, start_teleport=self.start_teleport())
            self.refresh_routes(select=path.stem)
            os.startfile(str(path))
            self.message.emit(f"已新建路线：{path.stem}")
        except Exception as exc:
            self.message.emit(f"新建路线失败：{exc}")

    def _copy_selected(self) -> None:
        source = self.selected_name()
        if not source:
            return
        name = self._ask_name("复制路线", f"{source}_副本")
        if not name:
            return
        try:
            path = self.store.duplicate(source, name)
            self.refresh_routes(select=path.stem)
            self.message.emit(f"已复制路线：{path.stem}")
        except Exception as exc:
            self.message.emit(f"复制路线失败：{exc}")

    def _rename_selected(self) -> None:
        source = self.selected_name()
        if not source:
            return
        name = self._ask_name("重命名路线", source)
        if not name or name == source:
            return
        try:
            path = self.store.rename(source, name)
            self.refresh_routes(select=path.stem)
            self.message.emit(f"已重命名路线：{path.stem}")
        except Exception as exc:
            self.message.emit(f"重命名路线失败：{exc}")

    def _delete_selected(self) -> None:
        name = self.selected_name()
        if not name:
            return
        box = MessageBox("删除路线", "确认删除", self.window())
        box.yesButton.setText("确认")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        try:
            self.store.delete(name)
            self.refresh_routes()
            self.message.emit(f"已删除路线：{name}")
        except Exception as exc:
            self.message.emit(f"删除路线失败：{exc}")

    def _edit_selected(self) -> None:
        name = self.selected_name()
        if not name:
            return
        try:
            os.startfile(str(self.store.txt_path(name)))
            self.message.emit(f"已打开路线：{name}")
        except Exception as exc:
            self.message.emit(f"打开路线失败：{exc}")

    def _open_folder(self) -> None:
        try:
            os.startfile(str(self.store.directory))
            self.message.emit(f"已打开路线目录：{self.store.directory}")
        except Exception as exc:
            self.message.emit(f"打开路线目录失败：{exc}")

    @staticmethod
    def _load_teleport_names() -> tuple[str, ...]:
        '只读取轻量目标注册表，避免UI启动时加载地图特征图谱。'
        asset_dir = Path(__file__).resolve().parents[1] / "assets" / "world_map"
        names: set[str] = set()
        for file_name in ("targets_v1.json", "special_targets_v1.json"):
            path = asset_dir / file_name
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for item in raw.get("targets", ()):
                    name = str(item.get("name") or "").strip()
                    kind = str(item.get("kind") or "").strip().lower()
                    if name and kind != "destination":
                        names.add(name)
            except (OSError, ValueError, TypeError):
                continue
        collator = QCollator(QLocale(QLocale.Chinese, QLocale.China))
        collator.setCaseSensitivity(Qt.CaseInsensitive)
        collator.setNumericMode(True)
        return tuple(sorted(names, key=cmp_to_key(collator.compare)))


class RouteQuickRunCard(RouteQuickRunCardView):
    '独立任务页中的轻量路线运行入口。'

    playRequested = Signal(str, int, bool)
    stopRequested = Signal()
    catalogRefreshRequested = Signal(str)
    selectionChanged = Signal(str)
    message = Signal(str)

    _DESC = "选择并运行已录制路线"

    def __init__(self, parent=None, *, store: RouteStore | None = None) -> None:
        super().__init__(parent)
        self.store = store or RouteStore()
        self._mode = "idle"
        self._invalid_routes: dict[str, str] = {}
        self.loop_spin.setValue(max(1, int(cfg.get("route_loop_count") or 1)))

        self.play_btn.clicked.connect(self._request_play)
        self.stop_btn.clicked.connect(self._request_stop)
        self.refresh_btn.clicked.connect(self._request_refresh)
        self.route_combo.currentTextChanged.connect(self._on_selection_changed)
        self.loop_spin.valueChanged.connect(
            lambda value: cfg.set("route_loop_count", int(value)))
        self.refresh_routes()

    @property
    def mode(self) -> str:
        return self._mode

    def selected_name(self) -> str:
        return str(self.route_combo.currentText() or "").strip()

    def select_route(self, name: str) -> None:
        '无递归信号地同步完整工作台的当前路线。'
        text = str(name or "").strip()
        if not text or text == self.selected_name():
            return
        index = self.route_combo.findText(text)
        if index < 0:
            return
        self.route_combo.blockSignals(True)
        self.route_combo.setCurrentIndex(index)
        self.route_combo.blockSignals(False)
        self._on_selection_changed(text)

    def refresh_routes(self, *, select: str = "", announce: bool = False) -> None:
        '刷新轻量入口；不会创建另一套运行实例。'
        if self._mode != "idle":
            if announce:
                self.message.emit("路线运行或录制中，暂不能刷新")
            return
        preferred = str(select or self.selected_name()
                        or cfg.get("route_script_file") or "")
        _routes, errors = self.store.sync_all()
        names = self.store.list_names()
        self._invalid_routes = dict(errors)
        self.route_combo.blockSignals(True)
        self.route_combo.clear()
        self.route_combo.addItems(list(names))
        if preferred in names:
            self.route_combo.setCurrentText(preferred)
        elif names:
            self.route_combo.setCurrentText(names[0])
        self.route_combo.blockSignals(False)
        self._on_selection_changed(self.selected_name())
        if announce:
            if errors:
                self.message.emit(
                    f"刷新完成：{len(names)}条路线，{len(errors)}条格式无效")
            else:
                self.message.emit(f"刷新完成：已同步{len(names)}条路线")

    def set_playing(self) -> None:
        self._mode = "playing"
        self._set_content("运行中…")
        self._update_controls()

    def set_paused(self, paused: bool) -> None:
        if self._mode not in {"playing", "paused"}:
            return
        self._mode = "paused" if paused else "playing"
        self._set_content("已暂停" if paused else "运行中…")
        self._update_controls()

    def set_recording(self) -> None:
        self._mode = "recording"
        self._set_content("路线录制中 · 按F12结束并保存")
        self._update_controls()

    def set_stopping(self, text: str = "停止中…") -> None:
        self._mode = "stopping"
        self._set_content(text)
        self._update_controls()

    def reset_runtime(self, text: str = "") -> None:
        self._mode = "idle"
        self._set_content(text or self._DESC)
        self._update_controls()

    def set_runtime_state(self, text: str) -> None:
        if self._mode != "idle" and text:
            self._set_content(str(text))

    def mark_invalid(self, name: str, error: str) -> None:
        self._invalid_routes[str(name)] = str(error)
        if self.selected_name() == str(name):
            self._set_content(f"配置无效：{error}")
            self._update_controls()

    def set_progress(self, current: int, total: int) -> None:
        if self._mode in {"playing", "paused"} and total > 0:
            prefix = "已暂停" if self._mode == "paused" else "运行中"
            self._set_content(f"{prefix} · {int(current)}/{int(total)}")

    def shutdown(self) -> None:
        self._mode = "stopping"
        self.setEnabled(False)

    def _set_content(self, text: str) -> None:
        '独立任务卡保持固定说明，运行状态显示在页面底部。'

    def _update_controls(self) -> None:
        idle = self._mode == "idle"
        selected_name = self.selected_name()
        selected = bool(selected_name)
        valid = selected and selected_name not in self._invalid_routes
        playing = self._mode in {"playing", "paused"}
        recording = self._mode == "recording"
        self.play_btn.setEnabled(idle and valid)
        self.stop_btn.setEnabled(playing or recording)
        self.route_combo.setEnabled(idle)
        self.refresh_btn.setEnabled(idle)
        self.loop_spin.setEnabled(idle)

    def _on_selection_changed(self, name: str) -> None:
        if name:
            cfg.set("route_script_file", str(name))
        error = self._invalid_routes.get(str(name))
        if self._mode == "idle":
            self._set_content(f"配置无效：{error}" if error else self._DESC)
        self._update_controls()
        self.selectionChanged.emit(str(name or ""))

    def _request_play(self) -> None:
        if self._mode != "idle":
            return
        name = self.selected_name()
        if not name:
            self.message.emit("没有可执行的路线")
            return
        try:
            route = self.store.load(name)
        except Exception as exc:
            self.mark_invalid(name, str(exc))
            self.message.emit(f"路线配置无效：{exc}")
            return
        self.playRequested.emit(
            name,
            int(self.loop_spin.value()),
            bool(route.metadata.coordinate_correction),
        )

    def _request_stop(self) -> None:
        if self._mode in {"playing", "paused", "recording"}:
            self.stopRequested.emit()

    def _request_refresh(self) -> None:
        self.refresh_routes(announce=True)
        self.catalogRefreshRequested.emit(self.selected_name())

class RouteScriptInterface(RouteScriptInterfaceView):
    '嵌入主窗口导航的路线工作台，不创建独立窗口。'

    playRequested = Signal(str, int, bool)
    stopRequested = Signal()
    recordRequested = Signal(str, str, bool)
    finishRecordRequested = Signal()
    recordingStateChanged = Signal(bool)
    catalogChanged = Signal(str)
    selectionChanged = Signal(str)
    message = Signal(str)

    def __init__(self, parent=None, *, store: RouteStore | None = None) -> None:
        card = RouteScriptCard(store=store)
        super().__init__(card, parent)

        self.card.playRequested.connect(self.playRequested.emit)
        self.card.stopRequested.connect(self.stopRequested.emit)
        self.card.recordRequested.connect(self.recordRequested.emit)
        self.card.finishRecordRequested.connect(self.finishRecordRequested.emit)
        self.card.catalogChanged.connect(self.catalogChanged.emit)
        self.card.selectionChanged.connect(self.selectionChanged.emit)
        self.card.message.connect(self.message.emit)
        self.message.connect(self.show_status)

    @property
    def store(self) -> RouteStore:
        return self.card.store

    @property
    def mode(self) -> str:
        return self.card.mode

    def selected_name(self) -> str:
        return self.card.selected_name()

    def refresh_routes(self, *, select: str = "", announce: bool = False) -> None:
        self.card.refresh_routes(select=select, announce=announce)

    def select_route(self, name: str) -> None:
        self.card.select_route(name)

    def set_playing(self) -> None:
        self.card.set_playing()

    def set_paused(self, paused: bool) -> None:
        self.card.set_paused(paused)

    def set_recording(self) -> None:
        self.card.set_recording()
        self.recordingStateChanged.emit(True)

    def set_stopping(self, text: str = "停止中…") -> None:
        self.card.set_stopping(text)

    def reset_runtime(self, text: str = "") -> None:
        self.card.reset_runtime(text)
        self.recordingStateChanged.emit(False)

    def request_record_from_hotkey(self) -> None:
        '把录制热键请求转交给路线控制卡。'
        self.card.request_record_from_hotkey()

    def request_play_from_hotkey(self) -> None:
        '把F11启动请求转交给路线控制卡。'
        self.card.request_play_from_hotkey()

    def set_runtime_state(self, text: str) -> None:
        self.card.set_runtime_state(text)

    def mark_invalid(self, name: str, error: str) -> None:
        self.card.mark_invalid(name, error)

    def set_progress(self, current: int, total: int) -> None:
        self.card.set_progress(current, total)

    def shutdown(self) -> None:
        self.card.shutdown()
