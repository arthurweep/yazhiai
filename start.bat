@echo off
chcp 65001 >nul
cd /d %~dp0
set PRESSFURNACE_MODE=real
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe main.py
) else (
  python main.py
)
pause
