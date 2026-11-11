@echo off
REM 🚨 비상정지 — 다음 사이클부터 모든 주문이 차단됩니다.
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe tools\control.py stop %*
pause
