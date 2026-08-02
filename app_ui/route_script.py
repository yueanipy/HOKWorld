'自定义路线的纯 Qt 视图组件。'
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    ExpandGroupSettingCard,
    FluentIcon as FIF,
    IconWidget,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SingleDirectionScrollArea,
    SpinBox,
    SubtitleLabel,
    SwitchButton,
    ToolButton,
)


class _ToggleDropMenuMixin:
    '让下拉框在同一位置再次点击时稳定收起。'

    def _init_toggle_drop_menu(self) -> None:
        self._suppress_next_open = False
        self._closing_from_toggle = False
        self._toggle_guard_timer = QTimer(self)
        self._toggle_guard_timer.setSingleShot(True)
        self._toggle_guard_timer.setInterval(250)
        self._toggle_guard_timer.timeout.connect(self._clear_toggle_guard)

    def _clear_toggle_guard(self) -> None:
        self._suppress_next_open = False

    def _toggleComboMenu(self) -> None:
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
        cursor_inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self.dropMenu = None
        if cursor_inside and not self._closing_from_toggle:
            self._suppress_next_open = True
            self._toggle_guard_timer.start()


class ToggleComboBox(_ToggleDropMenuMixin, ComboBox):
    '支持再次点击收起的只读下拉框。'

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_toggle_drop_menu()


class ToggleEditableComboBox(_ToggleDropMenuMixin, EditableComboBox):
    '支持再次点击收起的可编辑下拉框。'

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_toggle_drop_menu()


class SearchableEditableComboBox(ToggleEditableComboBox):
    '支持任意位置文字匹配的可输入下拉框。'

    def set_search_items(self, items: list[str] | tuple[str, ...]) -> None:
        values = [str(item) for item in items]
        self.clear()
        self.addItems(values)
        completer = QCompleter(values, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setMaxVisibleItems(10)
        self.setCompleter(completer)


def fit_button_text(
        button: PushButton, *, minimum: int = 88, expand: bool = False,
        padding: int = 68) -> None:
    '按字体、图标和样式为按钮保留完整文字宽度。'
    text_width = button.fontMetrics().horizontalAdvance(button.text())
    required = max(button.sizeHint().width(), text_width + padding)
    button.setMinimumWidth(max(minimum, required))
    horizontal = (
        QSizePolicy.Policy.Expanding if expand else QSizePolicy.Policy.Minimum)
    button.setSizePolicy(horizontal, QSizePolicy.Policy.Fixed)


class RouteNameDialog(MessageBoxBase):
    '路线名称输入视图。'

    def __init__(self, title: str, initial: str = "", parent=None) -> None:
        super().__init__(parent)
        self.route_name = ""
        self.title_label = SubtitleLabel(title, self)
        self.name_edit = LineEdit(self)
        self.name_edit.setPlaceholderText("输入路线名称")
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.setText(str(initial))
        self.name_edit.selectAll()
        self.error_label = CaptionLabel("", self)
        self.error_label.setStyleSheet("color:#d13438;")
        self.error_label.hide()
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.name_edit)
        self.viewLayout.addWidget(self.error_label)
        self.yesButton.setText("确认")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(430)
        self.name_edit.textChanged.connect(self.clear_error)
        self.name_edit.returnPressed.connect(self.yesButton.click)

    def clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def show_error(self, message: str) -> None:
        self.error_label.setText(str(message))
        self.error_label.show()


class RouteScriptCardView(ExpandGroupSettingCard):
    '路线管理页面的纯视图。'

    def __init__(self, parent=None) -> None:
        super().__init__(
            FIF.ROBOT,
            "自定义路线",
            "F11开始自动脚本，F12停止录制或自动脚本",
            parent,
        )

        self.play_btn = PrimaryPushButton(FIF.PLAY, "开始", self)
        self.stop_btn = PushButton(FIF.CLOSE, "停止", self)
        for button, minimum in (
            (self.play_btn, 88),
            (self.stop_btn, 88),
        ):
            fit_button_text(button, minimum=minimum, padding=44)
            self.addWidget(button)
        self.stop_btn.setEnabled(False)

        selector_host = QWidget(self)
        selector_layout = QHBoxLayout(selector_host)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(8)
        self.route_combo = ToggleComboBox(selector_host)
        self.route_combo.setMinimumWidth(240)
        self.route_combo.setFixedHeight(36)
        self.route_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.route_combo.setMaxVisibleItems(8)
        self.refresh_btn = ToolButton(FIF.SYNC, selector_host)
        self.refresh_btn.setFixedSize(36, 36)
        self.refresh_btn.setToolTip("重新扫描TXT并同步JSON")
        selector_layout.addWidget(self.route_combo, 1)
        selector_layout.addWidget(self.refresh_btn)
        self.addGroup(FIF.DOCUMENT, "路线", "选择需要录制或执行的路线", selector_host)

        self.teleport_combo = SearchableEditableComboBox(self)
        self.teleport_combo.setMinimumWidth(280)
        self.teleport_combo.setFixedHeight(36)
        self.teleport_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.teleport_combo.setPlaceholderText("空白表示从当前位置开始")
        self.addGroup(
            FIF.TILES,
            "起始传送点",
            "录制时写入路线；回放以TXT中的设置为准",
            self.teleport_combo,
        )

        edit_host = QWidget(self)
        self.edit_layout = QHBoxLayout(edit_host)
        self.edit_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_layout.setSpacing(8)
        edit_host.setMinimumWidth(472)
        self.edit_btn = PushButton(FIF.DOCUMENT, "编辑", edit_host)
        self.new_btn = PushButton(FIF.ADD, "新建", edit_host)
        self.copy_btn = PushButton(FIF.COPY, "复制", edit_host)
        self.rename_btn = PushButton("重命名", edit_host)
        self.edit_layout.addStretch(1)
        for button in (
                self.edit_btn, self.new_btn, self.copy_btn, self.rename_btn):
            button.setFixedHeight(34)
            fit_button_text(button, minimum=80, padding=44)
            self.edit_layout.addWidget(button)
        edit_host.setMinimumWidth(sum(
            button.minimumWidth() for button in (
                self.edit_btn, self.new_btn, self.copy_btn, self.rename_btn)
        ) + self.edit_layout.spacing() * 3)
        self.addGroup(FIF.DOCUMENT, "编辑路线", "修改TXT或创建独立副本", edit_host)

        file_host = QWidget(self)
        file_layout = QHBoxLayout(file_host)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)
        file_host.setMinimumWidth(300)
        self.delete_btn = PushButton(FIF.DELETE, "删除", file_host)
        self.folder_btn = PushButton(FIF.FOLDER, "目录", file_host)
        file_layout.addStretch(1)
        for button, minimum in ((self.delete_btn, 80), (self.folder_btn, 80)):
            button.setFixedHeight(34)
            fit_button_text(button, minimum=minimum, padding=44)
            file_layout.addWidget(button)
        self.addGroup(FIF.FOLDER, "文件管理", "管理TXT及同步生成的JSON", file_host)

        record_host = QWidget(self)
        record_layout = QHBoxLayout(record_host)
        record_layout.setContentsMargins(0, 0, 0, 0)
        record_host.setMinimumWidth(300)
        self.record_btn = PushButton(FIF.VIDEO, "开始录制", record_host)
        self.record_btn.setFixedHeight(34)
        self.record_btn.setToolTip("Alt+F10开始录制，F12结束并保存")
        fit_button_text(self.record_btn, minimum=96, padding=44)
        record_layout.addStretch(1)
        record_layout.addWidget(self.record_btn)
        self.addGroup(FIF.VIDEO, "路线录制", "按Alt+F10开始录制", record_host)

        self.loop_spin = SpinBox(self)
        self.loop_spin.setRange(1, 999)
        self.loop_spin.setMinimumWidth(160)
        self.addGroup(FIF.SYNC, "循环次数", "整条路线重复执行次数", self.loop_spin)

        self.coordinate_switch = SwitchButton(self)
        self.addGroup(
            FIF.TILES,
            "路线检查点",
            "记录小地图坐标，用于校验路线位置与到达时间",
            self.coordinate_switch,
        )

class RouteQuickRunCardView(ExpandGroupSettingCard):
    '独立任务页面的路线运行纯视图。'

    def __init__(self, parent=None) -> None:
        super().__init__(FIF.ROBOT, "自定义路线", "选择并运行已录制路线", parent)
        self.play_btn = PrimaryPushButton(FIF.PLAY, "开始", self)
        self.stop_btn = PushButton(FIF.CLOSE, "停止", self)
        for button, minimum in (
            (self.play_btn, 80),
            (self.stop_btn, 80),
        ):
            fit_button_text(button, minimum=minimum, padding=44)
            self.addWidget(button)
        self.stop_btn.setEnabled(False)

        selector_host = QWidget(self)
        selector_layout = QHBoxLayout(selector_host)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(8)
        self.route_combo = ToggleComboBox(selector_host)
        self.route_combo.setMinimumWidth(240)
        self.route_combo.setFixedHeight(36)
        self.route_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.route_combo.setMaxVisibleItems(8)
        self.refresh_btn = ToolButton(FIF.SYNC, selector_host)
        self.refresh_btn.setFixedSize(36, 36)
        self.refresh_btn.setToolTip("重新扫描路线文件")
        selector_layout.addWidget(self.route_combo, 1)
        selector_layout.addWidget(self.refresh_btn)
        self.addGroup(FIF.DOCUMENT, "路线", "选择要执行的路线", selector_host)

        self.loop_spin = SpinBox(self)
        self.loop_spin.setRange(1, 999)
        self.loop_spin.setMinimumWidth(160)
        self.addGroup(FIF.SYNC, "循环次数", "整条路线重复执行次数", self.loop_spin)


class RouteScriptInterfaceView(QWidget):
    '主窗口中的路线滚动页面。'

    def __init__(self, card: QWidget, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("routeScriptInterface")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = SingleDirectionScrollArea(self, orient=Qt.Vertical)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.enableTransparentBackground()
        self.view = QWidget(self.scroll)
        self.view.setObjectName("routeScriptView")
        self.view.setStyleSheet("#routeScriptView{background:transparent;}")
        self.vbox = QVBoxLayout(self.view)
        self.vbox.setContentsMargins(28, 24, 28, 24)
        self.vbox.setSpacing(14)
        self.card = card
        self.card.setParent(self.view)
        self.card.setExpand(True)
        self.vbox.addWidget(self.card)

        self.status_card = CardWidget(self.view)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(16, 10, 16, 10)
        status_layout.setSpacing(10)
        self.status_icon = IconWidget(FIF.SYNC, self.status_card)
        self.status_icon.setFixedSize(16, 16)
        self.status_label = BodyLabel("", self.status_card)
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_icon)
        status_layout.addWidget(self.status_label, 1)
        self.status_card.hide()
        self.vbox.addWidget(self.status_card)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.view)
        outer.addWidget(self.scroll)

    def show_status(self, text: str) -> None:
        '在路线页底部显示最新运行状态。'
        message = str(text or "").strip()
        if not message:
            return
        self.status_label.setText(message)
        self.status_card.show()
