'设置页面与名单编辑对话框。'
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ExpandGroupSettingCard,
    FluentIcon as FIF, HyperlinkButton, InfoBar, InfoBarPosition, MessageBox,
    MessageBoxBase, PrimaryPushButton, ProgressBar, PushButton,
    PushSettingCard, SettingCard, StrongBodyLabel, SubtitleLabel,
    SwitchButton, SwitchSettingCard, TextEdit, TitleLabel,
)

from config import cfg
from winenv import is_admin, relaunch_as_admin
from runtime_guard import atomic_write_text, dev_log
from version import __version__

from .shared import ScrollInterface

APP_VERSION = f"v{__version__}"

class ListEditDialog(MessageBoxBase):
    '点开才出现的名单编辑弹窗(编辑一个 .txt:一行一个, 注释)。'

    def __init__(self, file, title, tip, placeholder, parent=None) -> None:
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        cap = CaptionLabel(tip, self)
        cap.setWordWrap(True)
        self.edit = TextEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFixedSize(460, 300)
        try:
            self.edit.setPlainText(file.read_text(encoding="utf-8"))
        except Exception:
            pass
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(cap)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(520)

    def text(self) -> str:
        return self.edit.toPlainText()


class SettingsInterface(ScrollInterface):
    '设置(简洁卡片版,对齐实时检测):每项两行 = 标题 + 说明。'

    autoCloseScriptChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__("settingsInterface")

        root = self.vbox
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        root.addWidget(TitleLabel("设置"))


        self.whitelist_card = PushSettingCard(
            "编辑名单", FIF.ADD, "采集白名单", "优先采集名单中的目标")
        self.whitelist_card.clicked.connect(self._edit_whitelist)
        root.addWidget(self.whitelist_card)


        self.blacklist_card = PushSettingCard(
            "编辑名单", FIF.BROOM, "采集碰撞名单", "遇到名单中的目标时停止采集")
        self.blacklist_card.clicked.connect(self._edit_blacklist)
        root.addWidget(self.blacklist_card)

        self.auto_close_script_card = SwitchSettingCard(
            FIF.POWER_BUTTON, "自动关闭脚本", "游戏关闭后结束脚本")
        self.auto_close_script_card.setChecked(
            bool(cfg.get("close_script_when_game_exits")))
        self.auto_close_script_card.checkedChanged.connect(
            self._on_auto_close_script_changed)
        root.addWidget(self.auto_close_script_card)


        self.monthly_card = SwitchSettingCard(
            FIF.CALENDAR, "月卡奖励", "自动领取每日奖励")
        self.monthly_card.setChecked(bool(cfg.get("monthly_card_enabled")))
        self.monthly_card.checkedChanged.connect(
            lambda on: cfg.set("monthly_card_enabled", bool(on)))
        root.addWidget(self.monthly_card)


        if is_admin():
            self.status_card = SettingCard(
                FIF.UPDATE, "运行状态", "显示权限状态并支持 F12 急停")
        else:
            self.status_card = PushSettingCard(
                "以管理员重启", FIF.UPDATE, "运行状态",
                "显示权限状态并支持 F12 急停")
            self.status_card.clicked.connect(self._relaunch_admin)
        root.addWidget(self.status_card)

        root.addStretch(1)

    def _on_auto_close_script_changed(self, on: bool) -> None:
        '保存退出策略，并通知主窗口立即更新进程监控。'
        enabled = bool(on)
        cfg.set("close_script_when_game_exits", enabled)
        self.autoCloseScriptChanged.emit(enabled)


    def _edit_whitelist(self) -> None:
        from gather.recognizer import whitelist_file
        self._edit_list(
            whitelist_file(), "采集白名单", "一行一个",
            "一行一个,例如:\n某稀有材料\n某宝箱", "白名单")

    def _edit_blacklist(self) -> None:
        from gather.recognizer import blacklist_file
        self._edit_list(
            blacklist_file(), "采集碰撞名单", "一行一个",
            "一行一个,例如:\n某不想采的交互", "碰撞名单")

    def _edit_list(self, file, title, tip, placeholder, label) -> None:
        dlg = ListEditDialog(file, title, tip, placeholder, self.window())
        if not dlg.exec():
            return
        text = dlg.text()
        try:
            atomic_write_text(file, text, encoding="utf-8")
        except Exception as e:
            dev_log(f"名单保存失败: {file}", e)
            InfoBar.error("保存失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)
            return
        n = len([ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")])
        InfoBar.success("已保存", f"{label}已写入 {n} 条",
                        duration=3000, position=InfoBarPosition.TOP, parent=self)

    def _relaunch_admin(self) -> None:
        try:
            relaunch_as_admin()
        except Exception as e:
            InfoBar.error("无法提权重启", str(e), duration=4000,
                          position=InfoBarPosition.TOP, parent=self)

