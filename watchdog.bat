@echo off
REM 워치독 — 봇이 죽거나 멈추면 경보 (작업 스케줄러 5분 주기 권장)
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe tools\watchdog.py %*
