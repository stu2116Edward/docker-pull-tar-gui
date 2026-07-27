@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo Docker Image Puller GUI 打包脚本
echo ========================================
echo.

REM ---------- 检查 Python ----------
echo [1/4] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python 并添加到 PATH
    pause
    exit /b 1
)
echo ✅ Python 已安装
echo.

REM ---------- 安装依赖 ----------
echo [2/4] 安装依赖包...
pip install -r requirements.txt
if errorlevel 1 (
    echo 警告: 部分依赖安装失败，尝试继续...
)
pip install pyinstaller
if errorlevel 1 (
    echo 错误: PyInstaller 安装失败
    pause
    exit /b 1
)
echo ✅ 依赖安装完成
echo.

REM ---------- 清理旧构建 ----------
echo [3/4] 清理旧的构建文件...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "*.spec" del /q "*.spec"
echo.

REM ---------- 执行打包 ----------
echo [4/4] 开始打包 GUI 程序...
pyinstaller -F -w -i favicon.ico --name DockerPullGUI --add-data "logo.ico;." --add-data "settings.png;." --add-data "style.qss;." --clean --noconfirm docker_image_puller_gui.py

if errorlevel 1 (
    echo.
    echo ❌ 打包失败！请检查错误信息。
    pause
    exit /b 1
)

echo.
echo ========================================
echo ✅ 打包完成！
echo 输出文件: dist\DockerPullGUI.exe
echo ========================================
echo.

pause