'自动战斗页面与方案选择组件。'
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCursor, QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon as FIF,
    IndeterminateProgressRing, LineEdit, MenuAnimationType, MessageBox,
    MessageBoxBase, PrimaryPushButton, PushButton, StrongBodyLabel,
    SubtitleLabel, ToolButton, isDarkTheme,
)

from app_workers import CombatWorker
from config import cfg
from winenv import find_game_hwnd, is_foreground
from runtime_guard import dev_log, registry, release_known_keys

from .shared import (
    ScrollInterface, _LatestStatusBatcher, _minimize_for_task,
    _resume_realtime_for, _suspend_realtime_for,
)

class _CombatProfileNameDialog(MessageBoxBase):
    '输入并校验新战斗方案名称。'

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.profile_name = ""
        self.title_label = SubtitleLabel("新建战斗方案", self)
        self.name_edit = LineEdit(self)
        self.name_edit.setPlaceholderText("输入方案名")
        self.name_edit.setClearButtonEnabled(True)
        self.error_label = CaptionLabel("", self)
        self.error_label.setStyleSheet("color:#d13438;")
        self.error_label.hide()
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.name_edit)
        self.viewLayout.addWidget(self.error_label)
        self.yesButton.setText("确认")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(430)
        self.name_edit.textChanged.connect(self._clear_error)
        self.name_edit.returnPressed.connect(self.yesButton.click)
        QTimer.singleShot(0, self.name_edit.setFocus)

    def _clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def validate(self) -> bool:
        from combat.profile import normalize_profile_name, profile_dir

        try:
            profile_name = normalize_profile_name(self.name_edit.text())
            destination = profile_dir() / f"{profile_name}.txt"
            if destination.exists():
                raise FileExistsError(f"方案已存在：{destination.name}")
        except (ValueError, FileExistsError) as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return False
        self.profile_name = profile_name
        return True


class _CombatProfileMenuRow(QWidget):
    '自动战斗方案下拉中的紧凑选择行。'

    selected = Signal()
    deleteRequested = Signal()

    def __init__(
        self,
        file_name: str,
        width: int,
        height: int,
        show_separator: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._file_name = str(file_name)
        self.setFixedSize(max(96, width), max(36, height))
        self.setObjectName("combatProfileMenuRow")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 2, 6, 2)
        layout.setSpacing(10)
        self.name_label = BodyLabel(self._file_name, self)
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.name_label, 1)
        self.delete_button = ToolButton(FIF.DELETE, self)
        self.delete_button.setFixedSize(28, 28)
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.setToolTip(f"删除 {file_name}")
        layout.addWidget(self.delete_button)
        self.delete_button.clicked.connect(self.deleteRequested)
        self.separator = QWidget(self)
        self.separator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.separator.setFixedHeight(1)
        self.separator.setVisible(bool(show_separator))
        self._update_elided_text()
        self._set_hovered(False)

    def _place_separator(self) -> None:
        self.separator.setGeometry(12, self.height() - 1, max(0, self.width() - 24), 1)
        self.separator.raise_()

    def _update_elided_text(self) -> None:
        available = max(40, self.width() - 14 - 6 - 10 - 28)
        text = QFontMetrics(self.name_label.font()).elidedText(
            self._file_name,
            Qt.TextElideMode.ElideMiddle,
            available,
        )
        self.name_label.setText(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_text()
        self._place_separator()

    def _set_hovered(self, hovered: bool) -> None:
        if isDarkTheme():
            color = "rgb(55,55,55)" if hovered else "rgb(39,39,39)"
        else:
            color = "rgb(242,244,243)" if hovered else "rgb(255,255,255)"
        separator_color = (
            "rgba(255,255,255,24)" if isDarkTheme()
            else "rgba(0,0,0,20)"
        )
        self.setStyleSheet(
            "#combatProfileMenuRow {"
            f"background-color:{color};"
            "}"
        )
        self.separator.setStyleSheet(f"background-color:{separator_color};")
        self._place_separator()

    def enterEvent(self, event) -> None:
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hovered(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _CombatProfileComboBox(ComboBox):
    '展开后允许按文件名删除单个战斗方案。'

    deleteRequested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pending_delete_name = ""
        self._suppress_next_open = False
        self._closing_from_toggle = False
        self._delete_timer = QTimer(self)
        self._delete_timer.setSingleShot(True)
        self._delete_timer.timeout.connect(self._emit_pending_delete)
        self._toggle_guard_timer = QTimer(self)
        self._toggle_guard_timer.setSingleShot(True)
        self._toggle_guard_timer.setInterval(250)
        self._toggle_guard_timer.timeout.connect(self._clear_toggle_guard)

    def _clear_toggle_guard(self) -> None:
        self._suppress_next_open = False

    def _emit_pending_delete(self) -> None:
        file_name = self._pending_delete_name
        self._pending_delete_name = ""
        if file_name:
            self.deleteRequested.emit(file_name)

    def _request_delete(self, file_name: str) -> None:
        self._pending_delete_name = file_name
        self._closeComboMenu()
        self._delete_timer.start(0)

    def _select_item(self, index: int) -> None:
        self._onItemClicked(index)
        self._closeComboMenu()

    def _toggleComboMenu(self) -> None:
        '同一位置第二次点击只关闭弹层，不让关闭事件重新触发展开。'
        if self._suppress_next_open:
            self._suppress_next_open = False
            self._toggle_guard_timer.stop()
            return
        if self.dropMenu is not None:
            self._closing_from_toggle = True
            try:
                self._closeComboMenu()
            finally:
                self._closing_from_toggle = False
            return
        self._showComboMenu()

    def _onDropMenuClosed(self) -> None:
        '弹层关闭后立即断开引用，避免保留已销毁的 Qt 对象。'
        cursor_inside = self.rect().contains(
            self.mapFromGlobal(QCursor.pos())
        )
        self.dropMenu = None
        if cursor_inside and not self._closing_from_toggle:
            self._suppress_next_open = True
            self._toggle_guard_timer.start()

    def _populate_combo_menu(self, menu) -> None:
        view_width = max(120, self.width())
        max_visible = self.maxVisibleItems()
        scrollbar = menu.view.verticalScrollBar()
        scrollbar_width = (
            max(12, scrollbar.sizeHint().width())
            if (
                menu.view.verticalScrollBarPolicy()
                != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                and max_visible > 0
                and len(self.items) > max_visible
            )
            else 0
        )


        row_width = max(96, view_width - scrollbar_width - 32)
        for index, item in enumerate(self.items):
            row = _CombatProfileMenuRow(
                item.text,
                row_width,
                self.height(),
                index < len(self.items) - 1,
                menu,
            )
            row.setEnabled(item.isEnabled)
            row.selected.connect(
                lambda value=index: self._select_item(value)
            )
            row.deleteRequested.connect(
                lambda name=item.text: self._request_delete(name)
            )
            menu.addWidget(row, selectable=False)

    def _fit_combo_menu(self, menu) -> None:
        max_visible = self.maxVisibleItems()
        menu.setMaxVisibleItems(
            max_visible if max_visible > 0 else max(1, len(self.items))
        )
        margins = menu.layout().contentsMargins()
        view_width = max(120, self.width())
        menu.view.setFixedWidth(view_width)
        menu.view.setSpacing(0)
        menu.setFixedWidth(
            view_width + margins.left() + margins.right()
        )

    def _showComboMenu(self) -> None:
        if not self.items:
            return

        menu = self._createComboMenu()
        self._populate_combo_menu(menu)
        self._fit_combo_menu(menu)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu
        if self.currentIndex() >= 0 and menu.actions():
            menu.setDefaultAction(menu.actions()[self.currentIndex()])



        down = self.mapToGlobal(QPoint(0, self.height() - 4))
        down_height = menu.view.heightForAnimation(
            down,
            MenuAnimationType.DROP_DOWN,
        )
        up = self.mapToGlobal(QPoint(0, 10))
        up_height = menu.view.heightForAnimation(
            up,
            MenuAnimationType.PULL_UP,
        )
        if down_height >= up_height:
            menu.view.adjustSize(down, MenuAnimationType.DROP_DOWN)
            menu.exec(down, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(up, MenuAnimationType.PULL_UP)
            menu.exec(up, aniType=MenuAnimationType.PULL_UP)


class CombatInterface(ScrollInterface):
    '自动战斗配置与运行页面。'

    _MODE_TEXT = {
        "不自动释放": "disabled",
        "可直接大招": "immediate",
        "仅昏迷时大招": "stunned",
    }

    def __init__(self) -> None:
        super().__init__("combatInterface")
        self._worker: CombatWorker | None = None
        self._resume_realtime = False
        self._last_message = ""

        root = self.vbox
        root.setContentsMargins(34, 28, 34, 28)
        root.setSpacing(18)

        self.panel = CardWidget(self)
        self.panel.setMinimumHeight(190)
        panel = QVBoxLayout(self.panel)
        panel.setContentsMargins(26, 22, 26, 22)
        panel.setSpacing(16)

        heading = QHBoxLayout()
        self.scheme_label = StrongBodyLabel("主C / 辅助方案")
        heading.addWidget(self.scheme_label, 1)
        self.running_label = StrongBodyLabel("运行中")
        self.running_label.setStyleSheet("color:#159a77;")
        self.running_label.hide()
        self.refresh_profile_btn = PushButton(FIF.SYNC, "刷新")
        self.refresh_profile_btn.setFixedHeight(30)
        self.refresh_profile_btn.setFixedWidth(92)
        self.refresh_profile_btn.setToolTip("重新扫描方案并同步运行配置")
        heading.addWidget(self.running_label)
        heading.addWidget(self.refresh_profile_btn)
        panel.addLayout(heading)

        profile_row = QHBoxLayout()
        profile_row.setSpacing(10)
        profile_row.addWidget(BodyLabel("方案"))
        self.profile_combo = _CombatProfileComboBox()
        self.profile_combo.setMinimumWidth(260)
        self.profile_combo.setFixedHeight(34)
        self.profile_combo.setMaxVisibleItems(7)
        profile_row.addWidget(self.profile_combo, 1)
        self.open_profile_btn = PushButton(FIF.DOCUMENT, "编辑")
        self.new_profile_btn = PushButton(FIF.ADD, "新建")
        self.profile_folder_btn = PushButton(FIF.FOLDER, "文件夹")
        self.open_profile_btn.setToolTip("编辑当前方案")
        self.new_profile_btn.setToolTip("新建战斗方案")
        self.profile_folder_btn.setToolTip("打开方案文件夹")
        for button, width in (
            (self.open_profile_btn, 78),
            (self.new_profile_btn, 78),
            (self.profile_folder_btn, 92),
        ):
            button.setFixedHeight(34)
            button.setMinimumWidth(width)
        self.profile_folder_btn.setFixedWidth(92)
        profile_row.addWidget(self.open_profile_btn)
        profile_row.addWidget(self.new_profile_btn)
        profile_row.addWidget(self.profile_folder_btn)
        panel.addLayout(profile_row)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        mode_row.addWidget(BodyLabel("大招"))
        self.mode_combo = ComboBox()
        self.mode_combo.addItems(list(self._MODE_TEXT))
        saved_mode = str(cfg.get("combat_ultimate_mode") or "immediate")
        selected_text = next(
            (text for text, value in self._MODE_TEXT.items() if value == saved_mode),
            "可直接大招",
        )
        self.mode_combo.setCurrentText(selected_text)
        self.mode_combo.setFixedWidth(210)
        self.mode_combo.setFixedHeight(34)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "F11 开始")
        self.stop_btn = PushButton(FIF.PAUSE, "F12 停止")
        self.start_btn.setFixedHeight(34)
        self.stop_btn.setFixedHeight(34)
        self.stop_btn.setEnabled(False)
        mode_row.addWidget(self.start_btn)
        mode_row.addWidget(self.stop_btn)
        panel.addLayout(mode_row)

        root.addWidget(self.panel)

        self.status_card = CardWidget(self)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(18, 12, 18, 12)
        self.status_ring = IndeterminateProgressRing(self.status_card)
        self.status_ring.setFixedSize(20, 20)
        self.status_ring.hide()
        self.status_text = BodyLabel("当前方案已就绪")
        status_layout.addWidget(self.status_ring)
        status_layout.addWidget(self.status_text, 1)
        root.addWidget(self.status_card)
        root.addStretch(1)

        self._status_batch = _LatestStatusBatcher(self, self._apply_status)
        self._profile_refresh_scheduled = False
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        self.start_btn.clicked.connect(self._start_from_button)
        self.profile_combo.deleteRequested.connect(self._delete_profile)
        self.open_profile_btn.clicked.connect(self._open_profile)
        self.new_profile_btn.clicked.connect(self._create_profile)
        self.profile_folder_btn.clicked.connect(self._open_profile_folder)
        self.refresh_profile_btn.clicked.connect(self._refresh_profiles_manually)
        self.stop_btn.clicked.connect(self._stop)
        self._refresh_profiles()
        self._reload_profile()
        from combat.profile import profile_dir
        self._profile_watcher = QFileSystemWatcher(self)
        self._profile_watcher.addPath(str(profile_dir()))
        self._profile_watcher.directoryChanged.connect(
            self._schedule_profile_refresh
        )

    def start_from_hotkey(self) -> None:
        'GUI 主线程处理 F11，只允许游戏处于前台时启动。'
        self._start(require_game_foreground=True)

    def _start_from_button(self) -> None:
        '鼠标点击后最小化主窗口并进行一次游戏前台交接。'
        self._start(require_game_foreground=False)

    def _start(self, *, require_game_foreground: bool) -> None:
        if self._worker:
            self._append("自动战斗已经运行")
            return
        hwnd = find_game_hwnd(prefer_foreground=require_game_foreground)
        if not hwnd:
            self._append("未找到游戏窗口")
            return
        if require_game_foreground and not is_foreground(hwnd):
            self._append("请先切换到游戏前台，再按 F11")
            return
        try:
            from combat.profile import CombatProfile, compile_text_profile
            selected_path = self._selected_profile_path()
            compiled_path = compile_text_profile(selected_path)
            profile = CombatProfile.load(compiled_path)
        except Exception as exc:
            self._append(f"战斗配置无效：{exc}")
            return
        if not profile.has_any_action:
            self._append("战斗配置没有动作，请先打开配置文件填写释放顺序")
            return

        self._resume_realtime = _suspend_realtime_for(self, "自动战斗")
        ok, reason = registry.start("自动战斗")
        if not ok:
            _resume_realtime_for(self, "自动战斗", self._resume_realtime)
            self._resume_realtime = False
            self._append(reason)
            return
        mode = self._MODE_TEXT.get(self.mode_combo.currentText(), "immediate")
        self._worker = CombatWorker(mode, str(selected_path))
        self._worker.sig_log.connect(self._append)
        self._worker.sig_done.connect(self._on_done)
        registry.set_stopper("自动战斗", self._worker.stop)
        self.running_label.setText("运行中")
        self.running_label.show()
        self.refresh_profile_btn.hide()
        self.status_ring.show()
        self.stop_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.profile_combo.setEnabled(False)
        self.open_profile_btn.setEnabled(False)
        self.new_profile_btn.setEnabled(False)
        self.profile_folder_btn.setEnabled(False)
        self.refresh_profile_btn.setEnabled(False)
        self._append(f"开始自动战斗：{profile.name}")

        def launch_worker() -> None:
            if self._worker:
                self._worker.start(QThread.Priority.NormalPriority)

        if require_game_foreground:
            launch_worker()
        else:
            _minimize_for_task(
                self,
                self._append,
                handoff=True,
                after=launch_worker,
            )

    def _stop(self) -> None:
        if not self._worker:
            return
        self.stop_btn.setEnabled(False)
        self._append("自动战斗停止中…")
        self._worker.stop()

    def _on_done(self, ok: bool) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        registry.finish("自动战斗")
        resume_realtime = self._resume_realtime
        self._resume_realtime = False
        self.running_label.hide()
        self.refresh_profile_btn.show()
        self.status_ring.hide()
        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.profile_combo.setEnabled(True)
        self.open_profile_btn.setEnabled(True)
        self.new_profile_btn.setEnabled(True)
        self.profile_folder_btn.setEnabled(True)
        self.refresh_profile_btn.setEnabled(True)
        self._append("自动战斗已结束" if ok else "自动战斗已停止")
        _resume_realtime_for(self, "自动战斗", resume_realtime)

    def _on_mode_changed(self, text: str) -> None:
        cfg.set("combat_ultimate_mode", self._MODE_TEXT.get(text, "immediate"))

    def _open_profile(self) -> None:
        try:
            path = self._selected_profile_path()
            os.startfile(str(path))
            self._append(f"已打开方案：{path.name}")
        except Exception as exc:
            self._append(f"无法打开配置文件：{exc}")

    def _open_profile_folder(self) -> None:
        try:
            from combat.profile import profile_dir
            path = profile_dir()
            os.startfile(str(path))
            self._append(f"已打开方案文件夹：{path}")
        except Exception as exc:
            self._append(f"无法打开方案文件夹：{exc}")

    def _delete_profile(self, file_name: str) -> None:
        try:
            from combat.profile import list_profile_paths
            path = next(
                path
                for path in list_profile_paths()
                if path.name == file_name
            )
        except StopIteration:
            self._append(f"无法删除方案：文件不存在 {file_name}")
            self._refresh_profiles()
            return
        except Exception as exc:
            self._append(f"无法删除方案：{exc}")
            return

        box = MessageBox(
            "删除方案",
            f"确认删除“{path.stem}”？",
            self.window(),
        )
        box.yesButton.setText("确认删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        try:
            from combat.profile import delete_profile
            delete_profile(path)
            self._append(f"已删除方案：{path.stem}")
        except Exception as exc:
            self._append(f"删除方案失败：{exc}")
        finally:
            self._refresh_profiles()
            self._reload_profile()

    def _create_profile(self) -> None:
        dialog = _CombatProfileNameDialog(self.window())
        if not dialog.exec():
            return
        try:
            from combat.profile import create_blank_profile
            path = create_blank_profile(dialog.profile_name)
            self._refresh_profiles(path)
            os.startfile(str(path))
            self._append(f"已创建方案：{path.name}")
        except Exception as exc:
            self._append(f"无法创建方案：{exc}")

    def _refresh_profiles(
        self,
        selected_path=None,
    ) -> tuple[tuple[Path, ...], tuple[Path, ...], dict[str, str]]:
        from combat.profile import (
            choose_profile_path,
            list_profile_paths,
            sync_profile_jsons,
        )

        paths = list_profile_paths()
        compiled, removed, sync_errors = sync_profile_jsons(paths)
        for json_path in removed:
            dev_log(f"删除无TXT关联的战斗JSON:{json_path.name}")
        for file_name, error in sync_errors.items():
            dev_log(f"战斗方案同步失败:{file_name}: {error}")
        preferred = (
            Path(selected_path).name
            if selected_path
            else str(cfg.get("combat_profile_file") or "")
        )
        chosen = choose_profile_path(paths, preferred)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems([path.name for path in paths])
        names = [path.name for path in paths]
        if chosen is not None:
            self.profile_combo.setCurrentText(chosen.name)
        self.profile_combo.blockSignals(False)
        if names:
            cfg.set("combat_profile_file", self.profile_combo.currentText())
        if sync_errors:
            self._append(
                f"{len(sync_errors)}份战斗方案无效，已跳过JSON同步"
            )
        return paths, compiled, sync_errors

    def _refresh_profiles_manually(self) -> None:
        '重新扫描TXT方案，同步JSON并刷新下拉列表。'
        if self._worker:
            self._append("自动战斗运行中，暂不能刷新方案")
            return
        selected = self.profile_combo.currentText()
        previous_names = {item.text for item in self.profile_combo.items}
        if self.profile_combo.dropMenu is not None:
            self.profile_combo._closeComboMenu()
        paths, compiled, sync_errors = self._refresh_profiles(
            Path(selected) if selected else None
        )
        self._reload_profile()
        added = len({path.name for path in paths} - previous_names)
        if sync_errors:
            self._append(
                f"刷新完成：新增 {added} 份方案，"
                f"同步 {len(compiled)} 份，失败 {len(sync_errors)} 份"
            )
            return
        self._append(
            f"刷新完成：新增 {added} 份方案，已同步 {len(compiled)} 份配置"
        )

    def _schedule_profile_refresh(self, _directory: str = "") -> None:
        '目录内TXT变化后合并刷新，避免编辑器连续写入触发多次。'
        if self._profile_refresh_scheduled:
            return
        self._profile_refresh_scheduled = True
        QTimer.singleShot(180, self._refresh_profiles_after_change)

    def _refresh_profiles_after_change(self) -> None:
        self._profile_refresh_scheduled = False
        if self._worker:
            return
        selected = self.profile_combo.currentText()
        self._refresh_profiles(Path(selected) if selected else None)
        self._reload_profile()

    def _selected_profile_path(self):
        from combat.profile import list_profile_paths

        selected = self.profile_combo.currentText()
        for path in list_profile_paths():
            if path.name == selected:
                return path
        raise FileNotFoundError("未选择有效的战斗方案")

    def _on_profile_changed(self, file_name: str) -> None:
        if file_name:
            cfg.set("combat_profile_file", file_name)
            self._reload_profile()

    def _reload_profile(self) -> None:
        try:
            from combat.profile import CombatProfile
            profile = CombatProfile.load(self._selected_profile_path())
            self.scheme_label.setText(profile.role_summary)
            self._append(
                f"已读取方案：{profile.name}；{profile.role_summary}"
            )
        except Exception as exc:
            self.scheme_label.setText("主C / 辅助方案无效")
            self._append(f"战斗配置无效：{exc}")

    def _append(self, message: str) -> None:
        self._status_batch.push(str(message))

    def _apply_status(self, message: str) -> None:
        self._last_message = message
        self.status_text.setText(message)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")
        release_known_keys(self._append)


