@echo off
REM 백두코인봇 — 감시 모드 (주문 없음). 먼저 이걸로 며칠 관찰할 것.
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe live_monitor.py %*
pause
