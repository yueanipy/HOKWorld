@echo off
rem 仅打包出 dist\HOKWorld\HOKWorld.exe(onedir,无控制台 + 管理员清单 + 王者图标)。
rem 打包逻辑统一在 HOKWorld.spec;要连同生成安装器请改用 build_installer.ps1。
cd /d %~dp0
pyinstaller --noconfirm --clean HOKWorld.spec
echo.
echo 完成。可执行文件: dist\HOKWorld\HOKWorld.exe
pause
