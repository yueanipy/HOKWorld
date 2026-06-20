"""把项目根加入 sys.path,让测试能直接 import version/paths/config/updater 等顶层模块。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# GUI 相关测试在无显示环境下也能 import
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
