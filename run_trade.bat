@echo off
REM ⚠️⚠️ 실제 자금으로 주문합니다. run_paper.bat 로 충분히 관찰한 뒤에만 사용.
cd /d "%~dp0"
set PYTHONUTF8=1
echo ============================================
echo   실거래 모드 (실제 주문 발생)
echo   중단: Ctrl+C  또는  stop.bat
echo ============================================
timeout /t 5
.venv\Scripts\python.exe live_monitor.py --trade %*
pause
