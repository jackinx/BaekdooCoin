@echo off
REM ============================================================
REM  Windows 작업 스케줄러 등록 (macOS cron 대체)
REM   - update_data : 매시 5분 데이터 증분 갱신
REM   - live_monitor: 부팅 시 자동 시작 (감시 모드)
REM  ※ 관리자 권한으로 실행하지 않아도 됨(현재 사용자 스케줄).
REM ============================================================
cd /d "%~dp0"
set PYTHONUTF8=1
set HERE=%~dp0

echo [1/2] 데이터 갱신 작업 등록 (매시 5분)...
schtasks /Create /F /SC HOURLY /MO 1 /ST 00:05 /TN "BaekdooCoin-UpdateData" ^
  /TR "\"%HERE%update_data.bat\""

echo [2/2] 로그온 시 감시모드 자동 시작 등록...
schtasks /Create /F /SC ONLOGON /TN "BaekdooCoin-Paper" ^
  /TR "\"%HERE%run_paper.bat\""

echo.
echo 등록 완료. 확인:  schtasks /Query /TN "BaekdooCoin-UpdateData"
echo 삭제:            schtasks /Delete /TN "BaekdooCoin-UpdateData" /F
echo                  schtasks /Delete /TN "BaekdooCoin-Paper" /F
echo.
echo 실거래 자동 시작은 의도적으로 등록하지 않았습니다. 필요하면 run_trade.bat 로 수동 등록하세요.
pause
