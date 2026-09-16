#!/bin/zsh
# 백두코인봇 Watchdog — live_monitor.py 생존 감시 + Hang 감지 + 자동 재시작
# crontab에서 매 1분 호출: * * * * * /bin/zsh /Volumes/X31/workspace/BaekdooCoin/watchdog.sh

set -euo pipefail

PROJECT_DIR="/Volumes/X31/workspace/BaekdooCoin"
PYTHON_BIN="/opt/anaconda3/envs/DTB/bin/python"
PIDFILE="/tmp/baekdoocoin_live_monitor.pid"
LOGFILE="/tmp/baekdoocoin_live.log"
WATCHDOG_LOG="/tmp/baekdoocoin_watchdog.log"
MAX_SILENCE_SEC=120  # 2분간 로그 없으면 hang으로 판단

log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$WATCHDOG_LOG"
}

ALIVE=0

# ── 1차: PID 생존 ──
if [[ -f "$PIDFILE" ]]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "0")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        ALIVE=1
    fi
fi

# ── 2차: 로그파일 최근 수정 (프로세스가 hang 상태인지 감지) ──
if (( ALIVE )) && [[ -f "$LOGFILE" ]]; then
    NOW=$(date +%s)
    LOG_MTIME=$(stat -f %m "$LOGFILE" 2>/dev/null || echo 0)
    SILENCE=$(( NOW - LOG_MTIME ))
    if (( SILENCE > MAX_SILENCE_SEC )); then
        log_msg "HANG detected: PID alive but log silent for ${SILENCE}s → force kill & restart"
        kill -9 "$OLD_PID" 2>/dev/null || true
        rm -f "$PIDFILE"
        ALIVE=0
    fi
fi

if (( ALIVE )); then
    exit 0
fi

# ── 재시작 ──
log_msg "live_monitor dead — restarting..."

if [[ -f "$PIDFILE" ]]; then
    OLD=$(cat "$PIDFILE" 2>/dev/null || echo "0")
    kill "$OLD" 2>/dev/null || true
    rm -f "$PIDFILE"
fi

cd "$PROJECT_DIR"

if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

nohup "$PYTHON_BIN" live_monitor.py --trade --db data/upbit_v2.db >> "$LOGFILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"

log_msg "restarted — PID: $NEW_PID"

# 텔레그램 알림
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=🔄 백두코인봇 자동 재시작%0APID: ${NEW_PID}%0A$(date '+%m/%d %H:%M')" \
        > /dev/null 2>&1 || true
fi