# -*- coding: utf-8 -*-
"""워치독 (deadman) — 봇이 죽거나 멈추면 5분 안에 알린다.

작업 스케줄러에 5분 주기로 등록:  schtasks /Create /SC MINUTE /MO 5 ...
봇은 이벤트가 있을 때만 텔레그램을 보내므로, 프로세스가 죽으면 '침묵'한다.
워치독이 그 침묵을 감지해 경보를 낸다(감사 F12 / 운영 갭).

  python tools/watchdog.py            # 점검 후 문제 시 텔레그램 경보 + exit 1
  python tools/watchdog.py --quiet    # 문제 없으면 아무 것도 출력하지 않음
  python tools/watchdog.py --max-age 900   # 마지막 사이클 허용 지연(초)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paths                              # noqa: E402


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    except Exception:                     # noqa: BLE001
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _telegram(msg: str, token: str, chat_id: str) -> bool:
    if not (token and chat_id):
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:                     # noqa: BLE001
        return False


def check(max_age_s: int = 900, min_age_s: int = 60) -> List[str]:
    problems: List[str] = []
    pid_file = paths.RUNTIME_DIR / "live_monitor.pid"
    if not pid_file.exists():
        problems.append("봇 PID 파일 없음 — 미실행 상태")
    else:
        try:
            pid = int(pid_file.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if not pid_alive(pid):
            problems.append(f"봇 프로세스(PID {pid}) 죽음")

    state = paths.STATE_FILE
    if not state.exists():
        problems.append("engine_state.json 없음 — 한 번도 사이클을 돌지 않음")
    else:
        age = time.time() - state.stat().st_mtime
        if age > max_age_s:
            problems.append(f"마지막 상태 갱신 {age / 60:.0f}분 전 (> {max_age_s / 60:.0f}분)")
        elif age < min_age_s:
            pass                      # 방금 갱신됨 — 정상
    if paths.emergency_active():
        problems.append("🚨 킬스위치가 켜져 있음")
    if paths.paused():
        problems.append("⏸️ 일시정지 상태")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=int, default=900)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    ap.add_argument("--chat", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    args = ap.parse_args()

    problems = check(args.max_age)
    if not problems:
        if not args.quiet:
            print("✅ 워치독: 정상")
        return 0

    msg = "🚨 백두코인봇 워치독 경보\n" + "\n".join(f"- {p}" for p in problems)
    print(msg)
    sent = _telegram(msg, args.token, args.chat)
    if not sent:
        print("(텔레그램 미설정/실패 — 로그로만 보고)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
