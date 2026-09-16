#!/bin/bash
# 업비트 허용 IP 변경 감시 스크립트
# 현재 public IP를 확인하고, 변경되면 알림 파일을 남긴다.
STATE_FILE="/Volumes/X31/workspace/BaekdooCoin/data/current_ip.txt"
ALERT_FILE="/tmp/baekdoocoin_ip_changed.txt"

CUR_IP=$(curl -s --max-time 15 ifconfig.me)

if [ -z "$CUR_IP" ]; then
    echo "$(date '+%F %T') ERROR: IP 확인 실패" >> /tmp/baekdoocoin_ip_watch.log
    exit 1
fi

if [ -f "$STATE_FILE" ]; then
    OLD_IP=$(cat "$STATE_FILE")
    if [ "$CUR_IP" != "$OLD_IP" ]; then
        echo "IP 변경 감지: $OLD_IP → $CUR_IP ($(date '+%F %T'))" > "$ALERT_FILE"
        echo "$(date '+%F %T') CHANGED: $OLD_IP -> $CUR_IP" >> /tmp/baekdoocoin_ip_watch.log
        echo "$CUR_IP" > "$STATE_FILE"
    fi
else
    echo "$CUR_IP" > "$STATE_FILE"
    echo "$(date '+%F %T') INIT: $CUR_IP" >> /tmp/baekdoocoin_ip_watch.log
fi

echo "$CUR_IP"
