"""冻结环境自检(仅打包验证用):
构建主窗口(验证 Qt 插件 / Fluent 资源)、定位钓鱼模板、加载 OCR onnx 模型。
不进游戏、不发输入、不需要管理员;成功打印 SELFTEST OK 并以 0 退出。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    import paths
    # 1) 只读资源在冻结后能定位(走 sys._MEIPASS\_internal)
    assert paths.resource_path("assets", "app.ico").exists(), "assets/app.ico 缺失"
    assert paths.resource_path(
        "fishing", "templates", "raw", "ready_button.png").exists(), "钓鱼模板缺失"
    # 2) 用户数据目录可写(%LOCALAPPDATA%\HOKWorldScript)
    assert paths.user_data_dir().exists(), "用户数据目录创建失败"

    # 3) 构建 GUI(验证 PySide6 平台插件 + qfluentwidgets 资源)
    from PySide6.QtWidgets import QApplication
    qt = QApplication.instance() or QApplication(sys.argv)
    import app as appmod
    win = appmod.build_window()
    title = win.windowTitle()
    assert "HOKWorld" in title, f"窗口标题异常: {title}"

    # 4) 钓鱼识别器加载全部模板 + OCR 加载 onnx 模型(冻结后从 _internal 读)
    from fishing.matcher import FishingRecognizer, _get_ocr
    FishingRecognizer()
    _get_ocr()

    print(f"SELFTEST OK | title={title} | data={paths.user_data_dir()}")
    qt.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
