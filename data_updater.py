# -*- coding: utf-8 -*-
"""증분 데이터 갱신 (원본 data_updater.py 대응).

최신 200캔들만 받아 INSERT OR REPLACE → 크론/작업스케줄러에서 주기 실행.
  python data_updater.py                      # 기본 1d 4h 1h
  python data_updater.py --timeframe 1d
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datastore
from data_crawler import candles, _row
from config import MAJOR_10_TICKERS

log = logging.getLogger("baekdoocoin.updater")

# 갱신 주기(초) — 이보다 최근 데이터가 있으면 스킵
FRESH = {"5m": 240, "1h": 1800, "4h": 3600, "1d": 3600}


def default_timeframes():
    """기본 갱신 타임프레임 — 5m 포함(감사 DATA-21: 5m 미갱신이면 5M 슬리브가 낡은 데이터로 판단)."""
    return ["1d", "4h", "1h", "5m"]


def _age_seconds(ts: str) -> float:
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - d).total_seconds()


def update(db: str, tickers, timeframes, force: bool = False) -> int:
    total = 0
    for ticker in tickers:
        for tf in timeframes:
            last = datastore.last_timestamp(db, tf, ticker)
            if last and not force and _age_seconds(last) < FRESH.get(tf, 1800):
                continue
            try:
                batch = candles(ticker, tf)
            except Exception as e:              # noqa: BLE001
                log.error("%s %s 갱신 실패: %s", ticker, tf, e)
                continue
            rows = [r for r in (_row(ticker, c) for c in batch) if r]
            if rows:
                total += datastore.upsert(db, tf, rows)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "upbit.db"))
    ap.add_argument("--timeframe", nargs="*", default=default_timeframes())
    ap.add_argument("--tickers", nargs="*", default=MAJOR_10_TICKERS)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    datastore.init_db(args.db)
    n = update(args.db, args.tickers, args.timeframe, args.force)
    log.info("갱신 완료 — %d행", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
