'关于页面。'
from __future__ import annotations

from qfluentwidgets import BodyLabel, CaptionLabel, TitleLabel

from .shared import ScrollInterface

class AboutInterface(ScrollInterface):
    def __init__(self) -> None:
        super().__init__("aboutInterface")
        lo = self.vbox
        lo.setContentsMargins(28, 22, 28, 22)
        lo.addWidget(TitleLabel("关于"))
        lo.addWidget(BodyLabel("HOKWorld — 《王者荣耀世界》黑盒视觉自动化"))
        lo.addWidget(CaptionLabel("仅黑盒视觉 + 标准键鼠;不读内存/不注入/不改封包。"))
        lo.addStretch(1)


