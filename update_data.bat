@echo off
REM 증분 데이터 갱신 (작업 스케줄러에 등록 권장: 매시 정각)
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe data_updater.py %*
