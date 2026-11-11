# -*- coding: utf-8 -*-
"""기존 수집 DB → 신규 DB 임포트.

이 PC의 옵시디언 볼트에 이미 있는 업비트 실측 데이터(299종 KRW, 2017-09-25 ~)를
새 프로젝트 DB로 옮긴다. api.upbit.com 이 막혀 있어도 백테스트/드라이런이 가능해진다.

원본: Jack_vault_android/Trading/research/backtest_upbit.db (테이블 upbit_daily)
      컬럼 market, date(YYYY-MM-DD), open, high, low, close, volume, value(KRW)

  python tools/import_legacy_db.py                        # 10종만
  python tools/import_legacy_db.py --all-markets          # 299종 전부
  python tools/import_legacy_db.py --source <path> --target <path>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import datastore                       # noqa: E402
from config import MAJOR_10_TICKERS    # noqa: E402

DEFAULT_SOURCE = Path(r"C:\Users\jacki\Documents\Jack_vault_android\Trading\research\backtest_upbit.db")
DEFAULT_TARGET = ROOT / "data" / "upbit.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--all-markets", action="store_true")
    ap.add_argument("--timeframe", default="1d", choices=["1d"])
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"원본 DB 없음: {src}")
        return 1
    datastore.init_db(args.target)

    con = sqlite3.connect(str(src))
    markets = ([r[0] for r in con.execute("SELECT DISTINCT market FROM upbit_daily ORDER BY market")]
               if args.all_markets else MAJOR_10_TICKERS)
    total = 0
    for m in markets:
        rows = con.execute(
            "SELECT date, open, high, low, close, volume, value FROM upbit_daily "
            "WHERE market=? ORDER BY date", (m,)).fetchall()
        if not rows:
            print(f"  {m:<12} 원본에 없음 — 건너뜀")
            continue
        payload = [(m, f"{d}T00:00:00", float(o), float(h), float(l), float(c),
                    float(v), float(val)) for d, o, h, l, c, v, val in rows]
        n = datastore.upsert(args.target, args.timeframe, payload)
        total += len(payload)
        print(f"  {m:<12} {len(payload):>6}행  {payload[0][1][:10]} ~ {payload[-1][1][:10]}")
    con.close()
    print(f"\n임포트 완료 — {len(markets)}종 / {total}행 → {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
