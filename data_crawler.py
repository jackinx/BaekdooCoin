#!/usr/bin/env python3
"""
백두코인봇 Upbit 데이터 크롤러
========================
업비트 KRW 마켓의 OHLCV 데이터를 수집하여 SQLite DB에 저장합니다.
DTB_v2의 주식 데이터 저장 방식과 동일한 구조를 따릅니다.

사용법:
    # 모든 KRW 티커, 모든 타임프레임 수집
    python data_crawler.py

    # 특정 타임프레임만 수집
    python data_crawler.py --timeframe 1h 1d

    # 특정 티커만 수집
    python data_crawler.py --ticker KRW-BTC KRW-ETH

    # 특정 티커 + 특정 타임프레임
    python data_crawler.py --ticker KRW-BTC --timeframe 1d 1h

    # 프로그램에서 import하여 사용
    from data_crawler import UpbitCrawler
    crawler = UpbitCrawler()
    crawler.fetch_all()

API 제한: 초당 약 10회 호출 가능. 호출 간 0.1초 대기.
캔들 API: 한 번에 최대 200개 반환 → 페이지네이션으로 전체 히스토리 수집.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "upbit.db")
API_BASE = "https://api.upbit.com"
RATE_LIMIT_SLEEP = 0.1  # 초당 ~10회
MAX_CANDLES_PER_CALL = 200

# 타임프레임 설정
# (키, API URL 템플릿, 테이블명, 표시명)
TIMEFRAME_CONFIG = {
    "1m":  ("minutes/1",  "ohlcv_1m",  "1분봉"),
    "5m":  ("minutes/5",  "ohlcv_5m",  "5분봉"),
    "15m": ("minutes/15", "ohlcv_15m", "15분봉"),
    "1h":  ("minutes/60", "ohlcv_1h",  "1시간봉"),
    "4h":  ("minutes/240","ohlcv_4h",  "4시간봉"),
    "1d":  ("days",       "ohlcv_1d",  "1일봉"),
    "1w":  ("weeks",      "ohlcv_1w",  "1주봉"),
}

# 로깅
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upbit_crawler")

# ---------------------------------------------------------------------------
# 데이터베이스 유틸리티
# ---------------------------------------------------------------------------
DDL = """\
CREATE TABLE IF NOT EXISTS {table} (
    ticker      TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,  -- ISO 8601 UTC (YYYY-MM-DDTHH:MM:SS)
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,  -- 거래량 (코인 단위)
    volume_krw  REAL    NOT NULL,  -- 거래대금 (KRW)
    PRIMARY KEY (ticker, timestamp)
);
"""

INSERT_SQL = """\
INSERT OR REPLACE INTO {table}
    (ticker, timestamp, open, high, low, close, volume, volume_krw)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?);
"""


# Whitelist of valid table names — must match TIMEFRAME_CONFIG values exactly.
# Used to prevent SQL injection when table names are interpolated into queries.
_VALID_TABLES = frozenset(cfg[1] for cfg in TIMEFRAME_CONFIG.values())


def _validate_table(table: str) -> str:
    """Validate that a table name is in the allowed whitelist.

    Raises ValueError if the table name is not recognized, preventing
    SQL injection from untrusted table-name sources.
    """
    if table not in _VALID_TABLES:
        raise ValueError(
            f"Invalid table name: '{table}'. Allowed: {sorted(_VALID_TABLES)}"
        )
    return table


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """데이터베이스 초기화: 테이블 생성 및 인덱스 설정."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    for table in [cfg[1] for cfg in TIMEFRAME_CONFIG.values()]:
        _validate_table(table)
        conn.execute(DDL.format(table=table))
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ticker "
            f"ON {table}(ticker);"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ts "
            f"ON {table}(timestamp);"
        )
        # Composite index for the common query pattern (ticker, timestamp)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ticker_ts "
            f"ON {table}(ticker, timestamp);"
        )
    conn.commit()
    logger.info(f"DB 초기화 완료: {db_path}")
    return conn


# ---------------------------------------------------------------------------
# Upbit API 클라이언트
# ---------------------------------------------------------------------------
class UpbitCrawler:
    """업비트 KRW 마켓 OHLCV 데이터 수집기."""

    def __init__(self, db_path: str = DB_PATH, max_pages: int = 0):
        self.db_path = db_path
        self.max_pages = max_pages
        self.conn: Optional[sqlite3.Connection] = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "백두코인봇-Upbit-Crawler/1.0",
        })

    # ------------------------------------------------------------------
    # 데이터베이스
    # ------------------------------------------------------------------
    def _ensure_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = init_db(self.db_path)
        return self.conn

    def close(self):
        """DB 연결 종료."""
        if self.conn:
            self.conn.close()
            self.conn = None

    # ------------------------------------------------------------------
    # API 호출 (재시도 로직 포함)
    # ------------------------------------------------------------------
    def _api_get(self, endpoint: str, params: dict = None, max_retries: int = 5) -> dict:
        """
        Upbit API GET 호출. 실패 시 재시도.
        반환값은 JSON 디코딩된 dict 또는 list.
        """
        url = f"{API_BASE}/v1/{endpoint}"
        last_exc = None

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    # Rate limit — 길게 대기
                    wait = min(2 ** attempt, 60)
                    logger.warning(f"Rate limited (429). {wait}s 대기 후 재시도...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_retries:
                    wait = min(2 ** attempt, 30)
                    logger.warning(
                        f"API 호출 실패 ({attempt}/{max_retries}): {e}. "
                        f"{wait}s 후 재시도..."
                    )
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"API 호출 최대 재시도 초과: {url} params={params}"
                    ) from last_exc

        raise RuntimeError(f"API unreachable: {url}") from last_exc

    # ------------------------------------------------------------------
    # 티커 목록 조회
    # ------------------------------------------------------------------
    def get_krw_markets(self) -> list[str]:
        """KRW 마켓 티커 목록 반환 (예: ['KRW-BTC', 'KRW-ETH', ...])."""
        logger.info("KRW 마켓 목록 조회 중...")
        markets = self._api_get("market/all")
        krw = [
            m["market"]
            for m in markets
            if m["market"].startswith("KRW-")
        ]
        logger.info(f"KRW 마켓 {len(krw)}개 발견")
        return krw

    # ------------------------------------------------------------------
    # 특정 티커의 캔들 데이터 페치 (전체 히스토리)
    # ------------------------------------------------------------------
    def fetch_candles(
        self,
        market: str,
        timeframe: str,
        max_pages: int = 0,
    ) -> list[tuple]:
        """
        특정 티커의 전체 캔들 데이터를 페이지네이션으로 수집.

        Returns:
            [(ticker, timestamp, open, high, low, close, volume, volume_krw), ...]
        """
        endpoint_template, table_name, display_name = TIMEFRAME_CONFIG[timeframe]
        candles_all: list[tuple] = []

        # Upbit는 to 파라미터를 ISO 8601로 받음
        # 마지막 캔들 시각을 to로 넘겨서 페이지네이션
        to_param = None  # None이면 최신부터
        page = 0

        while True:
            if max_pages > 0 and page >= max_pages:
                break
            page += 1
            params = {"market": market, "count": MAX_CANDLES_PER_CALL}
            if to_param:
                params["to"] = to_param

            raw = self._api_get(f"candles/{endpoint_template}", params=params)
            if not raw:
                break

            for candle in raw:
                # Upbit 캔들 응답 형식:
                #   candle_date_time_utc / candle_date_time_kst
                #   opening_price, high_price, low_price, trade_price
                #   candle_acc_trade_volume, candle_acc_trade_price
                ts = candle.get("candle_date_time_utc", "")
                candles_all.append((
                    market,
                    ts,
                    float(candle["opening_price"]),
                    float(candle["high_price"]),
                    float(candle["low_price"]),
                    float(candle["trade_price"]),
                    float(candle["candle_acc_trade_volume"]),
                    float(candle["candle_acc_trade_price"]),
                ))

            # 마지막 캔들보다 하나 이전부터 가져오도록 설정
            if len(raw) < MAX_CANDLES_PER_CALL:
                break  # 더 이상 데이터 없음

            last_ts = raw[-1]["candle_date_time_utc"]
            to_param = last_ts
            time.sleep(RATE_LIMIT_SLEEP)

        return candles_all

    # ------------------------------------------------------------------
    # DB에 벌크 저장
    # ------------------------------------------------------------------
    def save_candles(self, table: str, rows: list[tuple]):
        """캔들 데이터를 DB에 벌크 삽입 (INSERT OR REPLACE)."""
        if not rows:
            return
        _validate_table(table)
        conn = self._ensure_conn()
        sql = INSERT_SQL.format(table=table)
        conn.executemany(sql, rows)
        conn.commit()

    # ------------------------------------------------------------------
    # 메인 수집 루틴
    # ------------------------------------------------------------------
    max_pages: int = 0  # 0 = unlimited

    def collect(
        self,
        tickers: list[str],
        timeframes: list[str],
    ):
        """
        지정된 티커 × 타임프레임 조합으로 데이터 수집.

        Args:
            tickers: KRW 마켓 티커 목록
            timeframes: 수집할 타임프레임 키 목록 (['1m','1h','1d', ...])
        """
        total_jobs = len(tickers) * len(timeframes)
        logger.info(f"수집 시작: {len(tickers)} 티커 × {len(timeframes)} 타임프레임 = {total_jobs} 작업")

        # 전체 진행 바
        # tqdm: TTY 아닌 경우에도 동작하도록 설정
        pbar = tqdm(total=total_jobs, desc="전체 진행", unit="job",
                    disable=None, file=sys.stderr)

        for ticker in tickers:
            for tf_key in timeframes:
                endpoint, table, display = TIMEFRAME_CONFIG[tf_key]
                pbar.set_description(f"{ticker} {display}")

                try:
                    candles = self.fetch_candles(ticker, tf_key, max_pages=self.max_pages)
                    if candles:
                        self.save_candles(table, candles)
                        tqdm.write(
                            f"  ✓ {ticker} {display}: {len(candles)} 캔들 저장됨"
                        )
                    else:
                        tqdm.write(f"  - {ticker} {display}: 데이터 없음")
                except Exception as e:
                    logger.error(f"  ✗ {ticker} {display}: {e}")

                pbar.update(1)
                time.sleep(RATE_LIMIT_SLEEP)

        pbar.close()
        logger.info("수집 완료!")

    # ------------------------------------------------------------------
    # 통계 출력
    # ------------------------------------------------------------------
    def stats(self):
        """DB 통계 출력."""
        conn = self._ensure_conn()
        print("\n" + "=" * 60)
        print("  Upbit OHLCV Database Statistics")
        print("=" * 60)
        for tf_key, (endpoint, table, display) in TIMEFRAME_CONFIG.items():
            _validate_table(table)
            try:
                cur = conn.execute(f"SELECT COUNT(DISTINCT ticker), COUNT(*) FROM {table}")
                tickers_cnt, rows_cnt = cur.fetchone()
                if rows_cnt > 0:
                    cur2 = conn.execute(
                        f"SELECT MIN(timestamp), MAX(timestamp) FROM {table}"
                    )
                    min_ts, max_ts = cur2.fetchone()
                    print(f"  {display:8s}  table={table:12s}  "
                          f"tickers={tickers_cnt:3d}  rows={rows_cnt:7d}  "
                          f"[{min_ts} ~ {max_ts}]")
            except sqlite3.OperationalError:
                pass
        print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="백두코인봇 Upbit OHLCV 데이터 크롤러",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python data_crawler.py                           # 전체 수집
  python data_crawler.py --timeframe 1h 1d         # 1시간봉 + 1일봉만
  python data_crawler.py --ticker KRW-BTC KRW-ETH  # 특정 티커만
  python data_crawler.py --stats                   # DB 통계만 출력
        """,
    )
    parser.add_argument(
        "--timeframe", "-t",
        nargs="+",
        choices=list(TIMEFRAME_CONFIG.keys()),
        default=None,
        help="수집할 타임프레임 (기본값: 전체)",
    )
    parser.add_argument(
        "--max-pages", "-p",
        type=int,
        default=0,
        help="티커당 최대 페이지 수 (0=무제한, 1페이지=200캔들). 초기 시드 시 유용.",
    )
    parser.add_argument(
        "--ticker", "-k",
        nargs="+",
        default=None,
        help="수집할 티커 (예: KRW-BTC KRW-ETH). 미지정 시 전체 KRW 마켓.",
    )
    parser.add_argument(
        "--db", "-d",
        default=DB_PATH,
        help=f"SQLite DB 경로 (기본값: {DB_PATH})",
    )
    parser.add_argument(
        "--stats", "-s",
        action="store_true",
        help="DB 통계만 출력하고 종료",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    crawler = UpbitCrawler(db_path=args.db, max_pages=args.max_pages)

    try:
        if args.stats:
            crawler.stats()
            return

        # 티커 결정
        tickers = args.ticker if args.ticker else crawler.get_krw_markets()

        if not tickers:
            logger.warning("수집할 티커가 없습니다.")
            return

        # 타임프레임 결정
        timeframes = args.timeframe if args.timeframe else list(TIMEFRAME_CONFIG.keys())

        logger.info(f"타임프레임: {timeframes}")
        logger.info(f"티커 수: {len(tickers)}")

        crawler.collect(tickers, timeframes)
        crawler.stats()

    finally:
        crawler.close()


if __name__ == "__main__":
    main()