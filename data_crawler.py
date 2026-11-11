# -*- coding: utf-8 -*-
"""업비트 공개 캔들 수집기 (인증 불필요).

- 최초 1회 전체 히스토리:  python data_crawler.py
- 특정 타임프레임만:        python data_crawler.py --timeframe 1d 1h
- 페이지 제한(테스트):      python data_crawler.py --max-pages 3
- DB 통계:                  python data_crawler.py --stats

원본 data_crawler_v2.py 의 200봉 제한 회피 로직('to' 역방향 페이징)을 그대로 사용.
pandas 미사용 → requests 만으로 동작.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datastore
from config import MAJOR_10_TICKERS

log = logging.getLogger("baekdoocoin.crawler")
BASE = "https://api.upbit.com"
KST = timezone(timedelta(hours=9))
PATH = {"1d": "/v1/candles/days", "4h": "/v1/candles/minutes/240",
        "1h": "/v1/candles/minutes/60", "5m": "/v1/candles/minutes/5"}


def _get(url: str, params: dict, timeout: int = 20):
    last = None
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 baekdoocoin"})
            if r.status_code == 429:
                time.sleep(min(2 ** attempt, 60))
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                     # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"수집 실패: {url} {params} → {last}")


def market_list() -> List[str]:
    data = _get(f"{BASE}/v1/market/all", {})
    return [d["market"] for d in data if d["market"].startswith("KRW-")]


def candles(ticker: str, tf: str, to: Optional[str] = None, count: int = 200) -> List[dict]:
    params = {"market": ticker, "count": str(count)}
    if to:
        params["to"] = to
    return _get(f"{BASE}{PATH[tf]}", params)


def _row(ticker: str, c: dict) -> tuple:
    ts = c.get("candle_date_time_utc")
    if not ts:
        return ()
    return (ticker, ts, float(c["opening_price"]), float(c["high_price"]), float(c["low_price"]),
            float(c["trade_price"]), float(c["candle_acc_trade_volume"]),
            float(c["candle_acc_trade_price"]))


def fetch_ticker(ticker: str, tf: str, db_path: str, max_pages: int = 60,
                 start: str = "2017-01-01") -> int:
    """역방향 페이징으로 히스토리 전체 수집. 반환=저장 행 수."""
    to: Optional[str] = None
    total = 0
    for page in range(max_pages):
        batch = candles(ticker, tf, to=to)
        if not batch:
            break
        rows = [r for r in (_row(ticker, c) for c in batch) if r]
        rows = [r for r in rows if r[1][:10] >= start]
        if rows:
            total += datastore.upsert(db_path, tf, rows)
        oldest = min(c["candle_date_time_utc"] for c in batch)
        if oldest[:10] <= start:
            break
        to = oldest
        time.sleep(0.15)
    log.info("%s %s → %d행", ticker, tf, total)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "upbit.db"))
    ap.add_argument("--timeframe", nargs="*", default=["1d"])
    ap.add_argument("--tickers", nargs="*", default=MAJOR_10_TICKERS)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    datastore.init_db(args.db)

    if args.stats:
        for r in datastore.stats(args.db):
            print(f"{r['timeframe']:>3} {r['ticker']:<10} {r['rows']:>7}행  {r['from']} ~ {r['to']}")
        return 0

    grand = 0
    for ticker in args.tickers:
        for tf in args.timeframe:
            try:
                grand += fetch_ticker(ticker, tf, args.db, args.max_pages, args.start)
            except Exception as e:                 # noqa: BLE001
                log.error("%s %s 수집 실패: %s", ticker, tf, e)
    log.info("완료 — 총 %d행 저장 (db=%s)", grand, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
