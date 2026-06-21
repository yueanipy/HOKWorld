# HOKWorld — 《王者荣耀世界》黑盒视觉自动化

靠屏幕画面识别加标准键鼠模拟做两件事:**自动钓鱼**(抛竿、上钩、拉杆、收线、结算,带大鱼 QTE)和**剧情跳过**(认出剧情画面就自动跳过或推进,回到游戏立刻收手)。

只截屏、只发标准键鼠输入,不读写游戏内存、不注入、不改封包、不碰任何游戏文件。仅供学习研究,使用风险自负,请遵守游戏用户协议。

## 运行环境

- Windows 10 / 11(x64),Python 3.10 及以上
- 游戏以窗口或无边框窗口运行,并且保持前台不被遮挡(全靠截屏认画面)

## 跑起来

```bat
git clone git@github.com:yueanipy/HOKWorld.git
cd HOKWorld
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

启动时会弹 UAC 提权(游戏是提权运行的,发键鼠得用管理员),点「是」。运行中 **F12** 全局急停。

## 先看安全配置

默认是**演练模式**:只识别、不发任何键鼠。想真正操作游戏,去左侧「设置」打开「真实输入」并保持「演练」关闭。「时序抖动」默认关。

配置、日志、调试帧都写在 `%LOCALAPPDATA%\HOKWorldScript\`(`config.json`、`logs\hokworld.log` 等),不会落在源码目录里。

## 自动钓鱼

抛竿,等上钩,拉杆,收线,结算,接着续钓。大鱼会触发 QTE(快速连点 A/D,以及离散按钮 A/S/W/D);认出记录鱼就按 F 收进背包;落杆位置不对(过近、过远、不在水面、深度不够)会自动纠正;到等级上限或连续空抛太多次会优雅停机;结束打印一份统计。

## 剧情跳过(实时检测)

左侧「实时检测」点「开始」后实时读屏,每帧归到一个状态做唯一动作:

- 可跳过的剧情:ESC 调出确认框,再平滑移动光标点「跳过」
- 对话推进 / 点空白处继续 / 黑屏过场:原地点击
- 居中交互框(比如宝箱)、已经在游戏世界(右下 HUD 还在):不动

判断靠对话 UI 标志加右下 HUD 的黏滞门控,一回到游戏世界就立刻收手,不会误点、也不会乱按 ESC。沉浸式剧情默认不处理,需要的话可以在卡片里开「鼠标微动唤出控制条」(不太稳)。

## 目录结构

```
app.py              Fluent 控制台(PySide6 + qfluentwidgets,含安全设置页,自动提权)
version.py          版本与应用信息
paths.py            资源 / 用户数据路径(用户数据在 %LOCALAPPDATA%\HOKWorldScript)
config.py           配置与安全门控(演练 / 真实输入 / 时序抖动)
winenv.py           Windows 与游戏窗口工具(提权 / 隐藏控制台 / DPI / 窗口枚举)
applog.py           统一日志(logs\hokworld.log)
requirements.txt    依赖
assets/             界面 / 应用图标
fishing/
  matcher.py        钓鱼识别(模板 + 二值字形 IoU + HoughCircles + OCR)
  fisher.py         钓鱼状态机(发真实键鼠;演练模式只识别)
  template_bank.py  命名模板 + 逐特征预处理
  templates/raw/*   识别模板(从真实客户端帧裁切,必需,勿删)
story/
  recognizer.py     剧情识别(OCR 控制条 + 右下 HUD 边缘密度 + 黑屏比例)
  skipper.py        剧情跳过状态机(状态到动作,随机弧线移动光标)
```

## 几点提醒

- 模板是从 1920 宽的真实客户端帧裁的,运行时每帧先归一化到 1920 宽再匹配。分辨率、画质、HUD 差太多时阈值可能要重标定,改 `fishing/matcher.py` 里的阈值和 ROI。
- 只在游戏前台时动作,切走会自动暂停。
