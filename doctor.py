# -*- coding: utf-8 -*-
"""환경 진단 — 실행 전 점검 (python doctor.py)."""
from __future__ import annotations

import os
import platform
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "[OK]  ", "[WARN]", "[FAIL]"


def check_deps() -> bool:
    ok = True
    for mod in ("requests", "jwt", "websocket", "dotenv", "numpy"):
        try:
            m = __import__(mod)
            print(f"  {OK} {mod:<10} {getattr(m, '__version__', '')}")
        except Exception as e:                          # noqa: BLE001
            print(f"  {FAIL} {mod:<10} {e}")
            ok = False
    return ok


def check_net(host: str = "api.upbit.com", port: int = 443, timeout: float = 6.0) -> bool:
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {host} DNS 실패: {e}")
        return False
    t0 = time.time()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            print(f"  {OK} {host}:{port} 연결 ({ip}, {(time.time() - t0) * 1000:.0f}ms)")
            return True
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {host}:{port} 연결 실패 — {e}  (IP {ip})")
        return False


def main() -> int:
    print("=== 1. 시스템 ===")
    print(f"  Python {sys.version.split()[0]} ({platform.machine()}) · {platform.platform()}")
    print(f"  실행 경로 {sys.executable}")
    print(f"  프로젝트 {ROOT}")

    print("\n=== 2. 패키지 ===")
    deps_ok = check_deps()

    print("\n=== 3. 설정 (.env) ===")
    from config import Config
    cfg = Config.load()
    print(f"  업비트 키 : {'설정됨' if cfg.has_upbit() else '미설정 (모의 실행만 가능)'}")
    print(f"  텔레그램  : {'설정됨' if cfg.has_telegram() else '미설정 (원격제어 불가)'}")
    print(f"  DB        : {cfg.db_path}")
    print(f"  종목 {len(cfg.tickers)}개: {', '.join(t.replace('KRW-', '') for t in cfg.tickers)}")

    print("\n=== 4. 데이터 ===")
    import datastore
    try:
        rows = datastore.stats(cfg.db_path)
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} DB 읽기 실패: {e}")
        rows = []
    if not rows:
        print(f"  {WARN} DB 비어 있음 → data_crawler.py 또는 tools/import_legacy_db.py")
    for r in rows:
        if r["ticker"] in cfg.tickers:
            print(f"  {OK} {r['timeframe']:>3} {r['ticker']:<10} {r['rows']:>6}행  ~{r['to'][:10]}")

    print("\n=== 5. 네트워크 (업비트 REST) ===")
    rest_ok = check_net("api.upbit.com")
    if not rest_ok:
        print(f"  {WARN} 업비트 API 도달 불가 → 실거래/데이터 수집 불가. VPN(한국 노드) 또는 다른 네트워크 필요.")
        print("         (기존 DB로 백테스트/리플레이/모의 실행은 가능)")

    print("\n=== 6. 안전장치 ===")
    import paths
    paths.ensure_dirs()
    print(f"  런타임 디렉터리 : {paths.RUNTIME_DIR}")
    print(f"  킬스위치        : {'🚨 존재 (거래 중단 상태)' if paths.emergency_active() else '없음 (정상)'}")
    print(f"  포지션 파일     : {'있음 — ' + str(paths.POSITIONS_FILE) if paths.POSITIONS_FILE.exists() else '없음'}")

    print("\n=== 결론 ===")
    if deps_ok and rows:
        print("  실행 준비 완료. 먼저 'run_paper.bat'(감시 모드)로 신호를 관찰하세요.")
    else:
        print("  위 항목을 먼저 해결하세요.")
    return 0 if (deps_ok and rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
