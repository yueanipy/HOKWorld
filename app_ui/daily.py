'每日任务页面与任务排序组件。'
from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, ExpandGroupSettingCard,
    FluentIcon as FIF, IconWidget, InfoBar, InfoBarPosition,
    PrimaryPushButton, ProgressBar, PushButton, StrongBodyLabel,
    SwitchButton,
)

from app_workers import DailyWorker
from config import cfg
from winenv import is_admin
from runtime_guard import dev_log, registry, release_known_keys

from .shared import (
    ScrollInterface, _LatestStatusBatcher, _minimize_for_task,
    _resume_realtime_for, _suspend_realtime_for,
)

class _TaskRow(CardWidget):
    '一条龙里的一个任务行(参考绝区零一条龙):拖拽手柄 + 开关 + 名称 + 右侧留白(将来放。'

    def __init__(self, task_id: str, name: str, enabled: bool, parent=None) -> None:
        super().__init__(parent)
        self.task_id = task_id
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)
        self.handle = CaptionLabel("⠿")
        self.handle.setToolTip("拖动调整执行顺序")
        self.switch = SwitchButton()
        self.switch.setChecked(enabled)
        self.label = BodyLabel(name)
        lay.addWidget(self.handle)
        lay.addWidget(self.switch)
        lay.addWidget(self.label, 1)

        self.actions = QWidget()
        self.actions_lay = QHBoxLayout(self.actions)
        self.actions_lay.setContentsMargins(0, 0, 0, 0)
        self.actions_lay.setSpacing(6)
        lay.addWidget(self.actions)


class _ReorderList(QListWidget):
    '可整行拖动排序的列表(InternalMove);拖放完成后发 orderChanged。'
    orderChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._press_pos = None
        self._reflow_pending = False

    def resizeEvent(self, event) -> None:
        '让 setItemWidget() 创建的任务方框始终跟随列表视口宽度。'
        super().resizeEvent(event)
        self.schedule_item_reflow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.schedule_item_reflow()

    def schedule_item_reflow(self) -> None:
        if self._reflow_pending:
            return
        self._reflow_pending = True
        QTimer.singleShot(0, self._reflow_item_widgets)

    def _reflow_item_widgets(self) -> None:
        self._reflow_pending = False
        width = max(1, self.viewport().width())
        for index in range(self.count()):
            item = self.item(index)
            widget = self.itemWidget(item)
            if widget is None:
                continue
            height = max(item.sizeHint().height(), widget.sizeHint().height())



            widget.setMaximumWidth(width)
            widget.resize(width, height)
            widget.updateGeometry()
        self.doItemsLayout()

    def mousePressEvent(self, e) -> None:
        try:
            self._press_pos = e.position().toPoint()
        except Exception:
            self._press_pos = e.pos()
        super().mousePressEvent(e)

    def startDrag(self, supportedActions) -> None:
        item = self.currentItem()
        widget = self.itemWidget(item) if item is not None else None
        if widget is None:
            super().startDrag(supportedActions)
            return
        try:
            pm = widget.grab()

            rect = self.visualItemRect(item)
            if self._press_pos is not None:
                hot = self._press_pos - rect.topLeft()
                hot.setX(max(0, min(pm.width() - 1, hot.x())))
                hot.setY(max(0, min(pm.height() - 1, hot.y())))
            else:
                hot = QPoint(24, pm.height() // 2)
            mime = self.model().mimeData([self.indexFromItem(item)])
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.setPixmap(pm)
            drag.setHotSpot(hot)
            drag.exec(supportedActions, Qt.MoveAction)
        except Exception as exc:
            dev_log("拖动影像生成失败,回退默认", exc)
            super().startDrag(supportedActions)

    def dropEvent(self, e) -> None:
        super().dropEvent(e)
        self.orderChanged.emit()


class DailyInterface(ScrollInterface):
    '每日任务一条龙(UI 参考绝区零一条龙:任务卡可开关、可上下调序)。'
    _DESC = "按顺序自动完成每日任务"

    def __init__(self) -> None:
        super().__init__("dailyInterface")
        self._worker: DailyWorker | None = None
        self._paused = False
        self._resume_realtime = False
        self._last_msg = ""
        from daily.config import (
            DailyConfig,
            DISPATCH_REGIONS,
            RANCH_OPEN_REGIONS,
            TASK_REGISTRY,
        )
        self._DailyConfig = DailyConfig
        self._TASK_REGISTRY = TASK_REGISTRY
        self._DISPATCH_REGIONS = DISPATCH_REGIONS
        self._RANCH_OPEN_REGIONS = RANCH_OPEN_REGIONS
        self.config = DailyConfig()

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)


        self.card = ExpandGroupSettingCard(FIF.CALENDAR, "每日任务一条龙", self._DESC, self)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始")
        self.pause_btn = PushButton(FIF.PAUSE, "暂停")
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.card.addWidget(self.start_btn)
        self.card.addWidget(self.pause_btn)
        self.card.addWidget(self.stop_btn)
        root.addWidget(self.card)


        self.launch_switch = SwitchButton()
        self.launch_switch.setChecked(bool(cfg.get("daily_auto_launch_game")))
        self.launch_switch.checkedChanged.connect(
            lambda on: cfg.set("daily_auto_launch_game", bool(on)))


        self._daily_option_widgets: list[QWidget] = [self.launch_switch]
        self.farm_route_combo = ComboBox()
        self.farm_route_combo.addItems(["第二列", "第五列"])
        farm_route = self.config.param("farm", "route", "第二列")
        self.farm_route_combo.setCurrentText(
            farm_route if farm_route in ("第二列", "第五列") else "第二列")
        self.farm_route_combo.setMinimumWidth(96)
        self.farm_seed_combo = self._blank_daily_combo("种子")

        farm_options = QWidget()
        farm_options_lay = QHBoxLayout(farm_options)
        farm_options_lay.setContentsMargins(0, 0, 0, 0)
        farm_options_lay.setSpacing(8)
        farm_options_lay.addWidget(CaptionLabel("路线"))
        farm_options_lay.addWidget(self.farm_route_combo)
        farm_options_lay.addWidget(CaptionLabel("种子"))
        farm_options_lay.addWidget(self.farm_seed_combo)
        self.card.addGroup(FIF.LEAF, "农贸作物", "选择行进路线与种植种子", farm_options)

        self.incubator_seed_combo = self._blank_daily_combo("种子")
        self.card.addGroup(
            FIF.TILES, "培养箱", "选择需要种植的种子", self.incubator_seed_combo)

        self.ranch_region_combo = ComboBox()
        self.ranch_region_combo.addItems(self._RANCH_OPEN_REGIONS)
        ranch_region = self.config.ranch_open_region()
        self.ranch_region_combo.setCurrentText(
            self._RANCH_OPEN_REGIONS[ranch_region - 1])
        self.ranch_region_combo.setMinimumWidth(116)
        self.card.addGroup(
            FIF.TILES,
            "牧场",
            "选择已经开放的最高区域",
            self.ranch_region_combo,
        )

        dispatch_options = QWidget()
        dispatch_options_lay = QHBoxLayout(dispatch_options)
        dispatch_options_lay.setContentsMargins(0, 0, 0, 0)
        dispatch_options_lay.setSpacing(8)
        self.dispatch_region_combos: list[ComboBox] = []
        selected_regions = self.config.dispatch_regions()
        for index, selected in enumerate(selected_regions):
            combo = ComboBox()
            combo.addItems(self._DISPATCH_REGIONS)
            combo.setCurrentText(selected)
            combo.setMinimumWidth(132)
            combo.setMaxVisibleItems(8)
            dispatch_options_lay.addWidget(CaptionLabel(f"地区{index + 1}"))
            dispatch_options_lay.addWidget(combo)
            self.dispatch_region_combos.append(combo)
        self.card.addGroup(
            FIF.ROBOT, "宠物派遣", "选择三个互不重复的派遣地区", dispatch_options)

        self.friends_target_combo = self._blank_daily_combo("好友")
        self.card.addGroup(
            FIF.PEOPLE, "好友家浇水", "选择浇水目标", self.friends_target_combo)

        self.alchemy_material_combo = self._blank_daily_combo("材料")
        self.card.addGroup(
            FIF.CALORIES, "制药", "选择制作材料", self.alchemy_material_combo)

        self.cooking_material_combo = self._blank_daily_combo("材料")
        self.card.addGroup(
            FIF.CAFE, "烹饪", "选择制作材料", self.cooking_material_combo)
        self.card.addGroup(
            FIF.GAME, "自动启动游戏", "进入角色界面后开始每日任务", self.launch_switch)

        self._daily_option_widgets.extend([
            self.farm_route_combo,
            self.farm_seed_combo,
            self.incubator_seed_combo,
            self.ranch_region_combo,
            *self.dispatch_region_combos,
            self.friends_target_combo,
            self.alchemy_material_combo,
            self.cooking_material_combo,
        ])
        self.farm_route_combo.currentTextChanged.connect(self._on_farm_route_changed)
        self.ranch_region_combo.currentTextChanged.connect(
            self._on_ranch_region_changed)
        for combo in self.dispatch_region_combos:
            combo.currentTextChanged.connect(self._on_dispatch_region_changed)
        self._sync_dispatch_region_choices(save=False)


        self.list_box = CardWidget()
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(12, 12, 12, 12)
        self.list_lay.setSpacing(8)
        title = StrongBodyLabel("任务(可拖动)")
        self.list_lay.addWidget(title)
        self.task_list = _ReorderList()
        self.task_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.task_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.task_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.setFrameShape(QListWidget.NoFrame)
        self.task_list.setStyleSheet("QListWidget{background:transparent;}")
        self.task_list.orderChanged.connect(self._on_reorder)
        self.list_lay.addWidget(self.task_list)
        root.addWidget(self.list_box)
        self._rows: list[_TaskRow] = []
        self._rebuild_rows()


        self.progress = ProgressBar()
        self.progress.setValue(0)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status_card = CardWidget()
        sl = QHBoxLayout(self.status_card)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        self._status_icon = IconWidget(FIF.CALENDAR, self.status_card)
        self._status_icon.setFixedSize(16, 16)
        self.status = BodyLabel("")
        sl.addWidget(self._status_icon)
        sl.addWidget(self.status, 1)
        self.status_card.hide()
        root.addWidget(self.status_card)
        root.addStretch(1)

        self._status_batch = _LatestStatusBatcher(self, self._apply_status_message)
        self.start_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self._stop)

    @staticmethod
    def _blank_daily_combo(kind: str) -> ComboBox:
        '创建待接数据源的空白选择框，不提前虚构种子、材料或好友选项。'
        combo = ComboBox()
        combo.addItem("")
        combo.setCurrentIndex(0)
        combo.setMinimumWidth(116)
        combo.setToolTip(f"{kind}选项将在后续功能中加入")
        return combo


    def _rebuild_rows(self) -> None:
        self.task_list.clear()
        self._rows = []
        for tid in self.config.order:
            row = _TaskRow(tid, self._TASK_REGISTRY.get(tid, tid), self.config.is_enabled(tid))
            row.switch.checkedChanged.connect(lambda on, t=tid: self._on_toggle(t, on))
            item = QListWidgetItem(self.task_list)
            item.setData(Qt.UserRole, tid)
            item.setSizeHint(QSize(0, row.sizeHint().height() + 8))
            self.task_list.addItem(item)
            self.task_list.setItemWidget(item, row)
            self._rows.append(row)

        h = sum(self.task_list.sizeHintForRow(i) for i in range(self.task_list.count())) + 8
        self.task_list.setFixedHeight(max(1, h))
        self.task_list.schedule_item_reflow()

    def _on_reorder(self) -> None:
        '拖放完成 → 读列表新顺序存盘 → 重建(确保行控件与新序一致)。'
        new_order = [self.task_list.item(i).data(Qt.UserRole) for i in range(self.task_list.count())]
        self.config.set_order(new_order)
        self._rebuild_rows()

    def _on_toggle(self, task_id: str, on: bool) -> None:
        self.config.set_enabled(task_id, on)

    def _on_farm_route_changed(self, route: str) -> None:
        if route in ("第二列", "第五列"):
            self.config.set_param("farm", "route", route)

    def _on_ranch_region_changed(self, region: str) -> None:
        if region in self._RANCH_OPEN_REGIONS:
            self.config.set_ranch_open_region(region)

    def _on_dispatch_region_changed(self, _region: str) -> None:
        self._sync_dispatch_region_choices(save=True)

    def _sync_dispatch_region_choices(self, save: bool) -> None:
        '禁用其它选择框中已经占用的派遣地区。'
        selected = [combo.currentText() for combo in self.dispatch_region_combos]
        for combo in self.dispatch_region_combos:
            current = combo.currentText()
            for index in range(combo.count()):
                name = combo.itemText(index)
                combo.setItemEnabled(index, name == current or name not in selected)
        if save and len(selected) == 3 and len(set(selected)) == 3:
            self.config.set_dispatch_regions(selected)

    def _set_rows_enabled(self, on: bool) -> None:

        self.task_list.setDragDropMode(
            QAbstractItemView.InternalMove if on else QAbstractItemView.NoDragDrop)
        for r in self._rows:
            r.switch.setEnabled(on)
        for widget in self._daily_option_widgets:
            widget.setEnabled(on)


    def _start(self) -> None:
        if self._paused and self._worker:
            self._toggle_pause()
            return
        if self._worker:
            return
        if not self.config.run_list():
            InfoBar.warning("没有启用的任务", "先在下方勾选要跑的每日任务", duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        self._resume_realtime = _suspend_realtime_for(self, "每日任务")
        ok, reason = registry.start("每日任务")
        if not ok:
            _resume_realtime_for(self, "每日任务", self._resume_realtime)
            self._resume_realtime = False
            InfoBar.warning("任务已在运行", reason, duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        if not is_admin():
            InfoBar.warning("需要管理员", "请以管理员权限重启后再开始。",
                            duration=4000, position=InfoBarPosition.TOP, parent=self)
        self._paused = False
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(True)
        self._set_rows_enabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self._show_status("每日任务一条龙启动中…")
        auto_launch = self.launch_switch.isChecked()
        cfg.set("daily_auto_launch_game", bool(auto_launch))
        self._worker = DailyWorker(auto_launch)
        self._worker.sig_log.connect(self._append)
        self._worker.sig_progress.connect(self._on_progress)
        self._worker.sig_done.connect(self._on_done)
        registry.set_stopper("每日任务", self._worker.stop)
        tick = _minimize_for_task(
            self, self._append, handoff=not auto_launch, after=self._worker.start)
        self._worker.set_initial_input_tick(tick)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)

    def _toggle_pause(self) -> None:
        if not self._worker:
            return
        self._paused = not self._paused
        self._worker.set_paused(self._paused)
        self.pause_btn.setText("继续" if self._paused else "暂停")
        self.start_btn.setEnabled(self._paused)
        self._append("已暂停" if self._paused else "已继续")

    def _stop(self) -> None:
        if self._worker:
            self._append("停止中…")
            self._worker.stop()

    def _on_done(self) -> None:
        if self._worker:
            self._worker.wait(1500)
            self._worker = None
        registry.finish("每日任务")
        resume_realtime = self._resume_realtime
        self._resume_realtime = False
        self._paused = False
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self._set_rows_enabled(True)
        _resume_realtime_for(self, "每日任务", resume_realtime)

    def emergency_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._append("F12 急停")
        release_known_keys(self._append)


    def _show_status(self, msg: str) -> None:
        self.status_card.show()
        self._status_batch.show_now(msg)

    def _append(self, msg: str) -> None:
        self._status_batch.push(msg)

    def _apply_status_message(self, msg: str) -> None:
        self._last_msg = msg
        self._set_status_text(msg)

    def _set_status_text(self, msg: str) -> None:
        '仅最终总结允许随可用宽度换行；运行过程日志继续保持单行。'
        is_summary = msg.strip().startswith("每日任务一条龙:完成")
        self.status.setWordWrap(is_summary)
        self.status.setText(msg)
        self.status.updateGeometry()
        layout = self.status_card.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.status_card.updateGeometry()
        self._refresh_responsive_layout()


