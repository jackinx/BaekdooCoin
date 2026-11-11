# -*- coding: utf-8 -*-
"""원격 없이 로컬에서 킬스위치/상태 제어.

  python tools/control.py stop      # 🚨 비상정지 (다음 사이클부터 모든 주문 중단)
  python tools/control.py resume    # 매매 재개
  python tools/control.py status    # 상태 요약
  python tools/control.py positions # 저장된 포지션(JSON) 그대로 출력
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paths                                        # noqa: E402


def status() -> int:
    from config import Config
    import datastore
    cfg = Config.load()
    print(f"런타임   : {paths.RUNTIME_DIR}")
    print(f"킬스위치 : {'🚨 ON (신규 진입 중단)' if paths.emergency_active() else 'OFF (정상)'}")
    pid_file = paths.RUNTIME_DIR / "live_monitor.pid"
    print(f"PID 파일 : {pid_file.read_text().strip() if pid_file.exists() else '없음 (미실행)'}")
    pos_file = paths.POSITIONS_FILE
    if pos_file.exists():
        data = json.loads(pos_file.read_text(encoding="utf-8"))
        print(f"포지션   : {len(data)}종")
        for t, p in data.items():
            qty = (p.get("extras") or {}).get("qty", 0)
            print(f"  {t:<10} [{p.get('sleeve')}] {qty:.8f}개 진입 {p.get('entry_price', 0):,.4f} "
                  f"units={p.get('units')} CTP={'ON' if p.get('ctp_active') else 'OFF'}")
    else:
        print("포지션   : 없음")
    rows = datastore.stats(cfg.db_path) if Path(cfg.db_path).exists() else []
    print(f"DB       : {cfg.db_path} ({len(rows)}개 티커·타임프레임)")
    return 0


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    paths.ensure_dirs()
    if cmd == "stop":
        paths.emergency_stop()
        print("🚨 비상정지 ON —", paths.EMERGENCY_STOP_FILE)
        return 0
    if cmd == "resume":
        paths.emergency_clear()
        paths.unpause()
        print("▶️  비상정지 해제 + 일시정지 해제 — 매매 재개")
        return 0
    if cmd == "pause":
        paths.pause()
        print("⏸️  일시정지 — 신규 진입 중단(청산은 계속). 재개: control.py resume")
        return 0
    if cmd == "positions":
        if paths.POSITIONS_FILE.exists():
            print(paths.POSITIONS_FILE.read_text(encoding="utf-8"))
        else:
            print("포지션 파일 없음")
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
