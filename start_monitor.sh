#!/bin/bash
# BaekdooCoin live_monitor launcher
cd /Volumes/X31/workspace/BaekdooCoin
export PATH=/opt/anaconda3/envs/DTB/bin:$PATH
set -a && source .env && set +a
exec /opt/anaconda3/envs/DTB/bin/python -u live_monitor.py --trade --db data/upbit_v2.db >> /tmp/baekdoocoin_live.log 2>&1