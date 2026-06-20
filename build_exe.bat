@echo off
rem 打包成 HOKWorld.exe:
rem   --uac-admin  exe 自带管理员清单 -> 双击时 UAC 弹窗显示 "HOKWorld.exe"
rem   --icon       exe 文件图标 / 任务栏 / 桌面快捷方式图标 = assets\app.ico(王者徽标)
rem   --windowed   无控制台窗口,只出 UI
rem   --add-data   把 assets 和钓鱼模板一并打进去(识别必需)
rem 产物:dist\HOKWorld\HOKWorld.exe
cd /d %~dp0
pyinstaller --noconfirm --windowed --name HOKWorld --icon assets\app.ico --uac-admin ^
  --add-data "assets;assets" ^
  --add-data "fishing\templates;fishing\templates" ^
  --collect-all qfluentwidgets ^
  --collect-all rapidocr_onnxruntime ^
  app.py
echo.
echo 完成。可执行文件: dist\HOKWorld\HOKWorld.exe
pause
