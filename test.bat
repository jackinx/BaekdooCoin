@echo off
REM 머지 게이트: 테스트 + 커버리지 통과해야 커밋 가능
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe tools\gate.py %*
pause
