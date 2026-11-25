@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 安装失败：请检查 Python / pip 网络或企业镜像配置。
  pause
  exit /b 1
)
echo.
echo 依赖安装完成。
pause
