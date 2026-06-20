# HOKWorld — 《王者荣耀世界》黑盒视觉自动化

基于**屏幕画面识别 + 标准键鼠模拟**的自动化工具，实现**自动钓鱼**（抛竿 → 上钩 → 拉杆 → 收线 → 结算，含大鱼 QTE）与**实时剧情跳过**（识别剧情态自动跳过 / 推进，回到游戏即停手）。

> **纯黑盒**：只截屏 + 发标准键鼠输入；**不读写游戏内存、不注入、不改封包、不动任何游戏文件**。仅供学习研究，使用风险自负，请遵守游戏用户协议。

---

## 两种使用方式

- **安装包（推荐，免装 Python）**：到 [Releases](https://github.com/yueanipy/HOKWorld/releases) 下载 `HOKWorldScript-<版本>-Setup.exe`，双击安装即可。无需自行安装 Python / PySide6 / OpenCV 等。
- **源码运行（开发）**：见文末「从源码运行 / 打包」。

## 运行环境

- **Windows 10 / 11 (x64)**
- 游戏《王者荣耀世界》以**窗口 / 无边框窗口**运行，且保持在**前台不被遮挡**（靠截屏识别）。

## 安装包使用

1. 下载并运行 `HOKWorldScript-<版本>-Setup.exe`。
2. 安装向导可**选择安装位置**、创建**开始菜单**快捷方式、可选**桌面**快捷方式。**安装本身不需要管理员**（默认装到当前用户目录）。
3. 启动 HOKWorld 时会弹 **UAC 提权**：游戏通常提权运行，普通权限下 Windows UIPI 会拦截键鼠，故主程序需以管理员运行（自动请求）。
4. **F12** 全局急停。
5. **覆盖升级**：直接装新版即可，旧版被覆盖；**用户配置与日志保留**。**卸载**走系统「应用」或开始菜单卸载项，同样不删用户数据。

### 默认安全配置（重要)

首次运行处于**演练模式**：**只识别、不发送任何键鼠**。这样可先确认识别正常、不会误操作。

- 到左侧「**设置**」页打开「**真实输入**」开关，脚本才会真正操作游戏（演练随之关闭）。
- 「**时序抖动（TimingJitter）**」默认关闭，可选开启（光标移动叠加随机微抖动）。

### 用户数据位置

配置 / 日志 / 缓存 / 下载的更新包都在 `%LOCALAPPDATA%\HOKWorldScript\`，**安装目录只放程序文件**：

```
config.json            配置(演练/真实输入/时序抖动/更新偏好)
logs\hokworld.log      统一运行日志(启动、任务进度、识别、更新、异常)
updates\               在线更新下载的安装器
sessions\ 屏幕截图\     仅开发模式才写的调试帧 / 成功截图
```

### 在线更新

「设置 · 更新」可**手动检查**，也可开「**启动时自动检查**」。流程：查 GitHub 最新 Release → 比对版本（**禁止降级**）→ 显示版本与更新说明 → 确认 → 下载（带**进度**、可**取消**）→ **SHA-256 校验** → 启动新安装器并退出本程序覆盖升级。可「**跳过此版本**」。下载/校验/启动失败都只提示、继续用当前版本（主程序绝不覆盖运行中的自身）。

## 功能（自动钓鱼）

- 自动循环：抛竿 → 等待上钩 → 拉杆 → 收线 → 结算 → 续钓。
- **大鱼 QTE**：快速连点态跟随 A/D 高频点按；离散 QTE 识别大圆按钮里的 A/S/W/D 字母，随机延迟后各按一次。
- **记录鱼**：识别「个人新纪录 / F 放入背包」并处理。
- **落杆错误自动纠正**：过近上移、过远下移、再不行重定位水域中央，连续失败则停机报原因。
- **等级上限**弹窗按计划停机；**抛竿看门狗**：连续空抛（疑似缺饵/朝向/钓点异常）优雅停机。
- 结束输出统计：已钓数 / 抛竿数 / 用时 / 速率。

## 功能（剧情跳过 · 实时检测）

左侧「**实时检测**」页点「开始」后实时读屏，**每帧归类为一个状态再做唯一动作**：

- **可跳过剧情**（右上「跳过」控制条）→ 按 **ESC** 调出确认框 → 平滑移动光标点「跳过」。
- **对话推进 / 点击空白处继续 / 黑屏过场** → 原地点击逐句推进。
- **居中交互框**（宝箱「点击开启」）/ **游戏世界**（右下技能 HUD 在）→ 完全不动作。
- **「剧情结束即停手」保证**：靠对话 UI 标志 + 右下 HUD 黏滞门控，回到游戏世界立即停手，绝不误点（自动攻击）或误按 ESC 开菜单。
- **沉浸式剧情**（控制条整段隐藏）默认不处理；可在卡片里开「**鼠标微动唤出控制条（不稳定）**」。

## 从源码运行 / 打包

```bat
git clone git@github.com:yueanipy/HOKWorld.git
cd HOKWorld
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

打成安装包（需先装 [Inno Setup 6](https://jrsoftware.org/isdl.php) 与 `pip install pyinstaller`）：

```bat
powershell -ExecutionPolicy Bypass -File build_installer.ps1
```

产物 `installer\Output\HOKWorldScript-<版本>-Setup.exe`（同时生成 `.sha256`）。只想出免装 exe 用 `build_exe.bat`（产物 `dist\HOKWorld\HOKWorld.exe`）。

> 版本号唯一来源是 `version.py` 的 `__version__`；GUI、安装器、在线更新检查都取自它。**发布**：打 `v<版本>` 的 GitHub Release，上传 `Setup.exe` 与 `Setup.exe.sha256`，更新说明写在 Release 正文。

## 目录结构

```
app.py                     Fluent 控制台(PySide6 + qfluentwidgets;含设置/更新页,自动提权)
version.py                 唯一版本与应用元信息(版本号 / 发布仓库)
paths.py                   资源/用户数据路径(冻结安全;用户数据在 %LOCALAPPDATA%\HOKWorldScript)
config.py                  配置与安全门控(演练/真实输入/时序抖动)
winenv.py                  Windows 与游戏窗口工具(提权/隐藏控制台/DPI/窗口枚举)
updater.py                 在线更新(GitHub Releases 检查/下载/SHA-256 校验/拉起安装器)
applog.py                  统一日志(logs\hokworld.log,轮转)
requirements.txt           依赖
HOKWorld.spec              PyInstaller onedir 打包配置(资源/插件/OCR 模型/管理员清单/图标)
build_installer.ps1        一键构建安装器(PyInstaller → Inno Setup → SHA-256)
build_exe.bat              仅打包出 HOKWorld.exe
installer/HOKWorldScript.iss  Inno Setup 安装器脚本(单用户/快捷方式/覆盖升级/保留用户数据)
selftest.py / selftest.spec   冻结环境自检(验证窗口/Qt 插件/模板/OCR 模型)
assets/                    界面/exe 图标
fishing/
  matcher.py               钓鱼识别(模板框架 + 二值字形 IoU + HoughCircles + OCR)
  fisher.py                钓鱼状态机(发真实键鼠;演练模式只识别)
  template_bank.py         命名模板 + 逐特征预处理小框架
  templates/raw/*.png      识别模板(从真实客户端帧裁切,必需,勿删)
story/
  recognizer.py            剧情识别(OCR 控制条 + 右下 HUD 边缘密度 + 黑屏比例 → classify)
  skipper.py               剧情跳过状态机(状态→动作;随机弧线移动光标)
tests/                     pytest(版本/路径/配置/更新逻辑)
```

## 注意事项

- 模板从 **1920 宽**真实客户端帧裁切，运行时每帧归一化到 1920 宽再匹配。分辨率 / 画质 / HUD 差异很大时识别阈值可能需重标定（调 `fishing/matcher.py` 的阈值/ROI）。
- 仅在游戏前台时动作；切走会自动暂停。
- 调试帧 / 成功截图仅**开发模式**（源码运行）才写入用户数据目录；安装版默认不落本地图片，只留统一日志。
