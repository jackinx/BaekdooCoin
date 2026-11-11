@echo off
REM 거래 원장 리포트 (성과/수수료/세금)
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe tools\report.py %*
pause
