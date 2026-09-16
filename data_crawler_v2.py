#!/usr/bin/env python3
"""
백두코인봇 Upbit 데이터 크롤러 v2
==============================
주요 10개 KRW 마켓의 OHLCV 데이터를 수집하여 SQLite DB(upbit_v2.db)에 저장합니다.
v1과 달리 상위 10개 티커만 집중 수집하며, 1일/4시간/1시간 타임프레임을 대상으로 합니다.

사용법:
    # 전체 수집 (1d + 4h + 1h, 10개 티커, 전체 히스토리)
    python data_crawler_v2.py

    # 특정 타임프레임만
    python data_crawler_v2.py --timeframe 1d 4h

    # 통계 확인
    python data_crawler_v2.py --stats

API 제한: 초당 약 10회 호출 가능. 호출 간 0.15초 대기.
캔들 API: 한 번에 최대 200개 반환 → 페이지네이션으로 전체 히스토리 수집.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from typing import Optional

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "upbit_v2.db")
API_BASE = "https://api.upbit.com"
RATE_LIMIT_SLEEP = 0.15  # 초당 ~6-7회 (보수적)
MAX_CANDLES_PER_CALL = 200

# 주요 10개 KRW 마켓 티커 (고정)
MAJOR_10_TICKERS = [
    "KRW-BTC",
    "KRW-ETH",
    "KRW-SOL",
    "KRW-XRP",
    "KRW-ADA",
    "KRW-DOGE",
    "KRW-AVAX",
    "KRW-DOT",
    "KRW-POL",   # MATIC → POL 리브랜딩
    "KRW-LINK",
]

# 타임프레임 설정 (v2: 1d, 4h, 1h 만)
TIMEFRAME_CONFIG = {
    "1h": ("minutes/60",  "ohlcv_1h",  "1시간봉"),
    "4h": ("minutes/240", "ohlcv_4h",  "4시간봉"),
    "1d": ("days",        "ohlcv_1d",  "1일봉"),
}

# 로깅
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upbit_crawler_v2")

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


def init_db(db_path: str = DB_PATH, drop_existing: bool = False) -> sqlite3.Connection:
    """데이터베이스 초기화: 기존 DB 삭제(선택) 후 테이블 및 인덱스 생성."""
    if drop_existing and os.path.exists(db_path):
        os.remove(db_path)
        # WAL 파일도 함께 제거
        for suffix in ["-wal", "-shm"]:
            wal_path = db_path + suffix
            if os.path.exists(wal_path):
                os.remove(wal_path)
        logger.info(f"기존 DB 삭제됨: {db_path}")

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-65536;")  # 64MB 캐시

    for table in [cfg[1] for cfg in TIMEFRAME_CONFIG.values()]:
        conn.execute(DDL.format(table=table))
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ticker "
            f"ON {table}(ticker);"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_ts "
            f"ON {table}(timestamp);"
        )
    conn.commit()
    logger.info(f"DB 초기화 완료: {db_path}")
    return conn


# ---------------------------------------------------------------------------
# Upbit API 클라이언트
# ---------------------------------------------------------------------------
class UpbitCrawlerV2:
    """업비트 KRW 마켓 OHLCV 데이터 수집기 v2 — 주요 10개 티커 전용."""

    def __init__(self, db_path: str = DB_PATH, max_pages: int = 0,
                 drop_existing: bool = False):
        self.db_path = db_path
        self.max_pages = max_pages
        self.drop_existing = drop_existing
        self.conn: Optional[sqlite3.Connection] = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "백두코인봇-Upbit-CrawlerV2/2.0",
        })

    # ------------------------------------------------------------------
    # 데이터베이스
    # ------------------------------------------------------------------
    def _ensure_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = init_db(self.db_path, drop_existing=self.drop_existing)
        return self.conn

    def close(self):
        """DB 연결 종료."""
        if self.conn:
            self.conn.close()
            self.conn = None

    # ------------------------------------------------------------------
    # API 호출 (재시도 로직 포함)
    # ------------------------------------------------------------------
    def _api_get(self, endpoint: str, params: dict = None,
                 max_retries: int = 5) -> list:
        """
        Upbit API GET 호출. 실패 시 지수 백오프 재시도.
        반환값은 JSON 디코딩된 list.
        """
        url = f"{API_BASE}/v1/{endpoint}"
        last_exc = None

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
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
    # 특정 티커의 캔들 데이터 페치 (전체 히스토리)
    # ------------------------------------------------------------------
    def fetch_candles(
        self,
        market: str,
        timeframe: str,
    ) -> list[tuple]:
        """
        특정 티커의 전체 캔들 데이터를 페이지네이션으로 수집.

        Returns:
            [(ticker, timestamp, open, high, low, close, volume, volume_krw), ...]
        """
        endpoint_template, table_name, display_name = TIMEFRAME_CONFIG[timeframe]
        candles_all: list[tuple] = []

        to_param = None  # None이면 최신부터
        page = 0
        total_fetched = 0

        while True:
            if self.max_pages > 0 and page >= self.max_pages:
                break
            page += 1
            params = {"market": market, "count": MAX_CANDLES_PER_CALL}
            if to_param:
                params["to"] = to_param

            raw = self._api_get(f"candles/{endpoint_template}", params=params)
            if not raw:
                break

            for candle in raw:
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

            batch_size = len(raw)
            total_fetched += batch_size

            # 마지막 캔들보다 하나 이전부터 가져오도록 설정
            if batch_size < MAX_CANDLES_PER_CALL:
                break  # 더 이상 데이터 없음

            # 가장 오래된 캔들의 시각을 to로 설정
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
        conn = self._ensure_conn()
        sql = INSERT_SQL.format(table=table)
        conn.executemany(sql, rows)
        conn.commit()

    # ------------------------------------------------------------------
    # 메인 수집 루틴
    # ------------------------------------------------------------------
    def collect(
        self,
        tickers: list[str],
        timeframes: list[str],
    ):
        """
        지정된 티커 × 타임프레임 조합으로 데이터 수집.

        Args:
            tickers: KRW 마켓 티커 목록
            timeframes: 수집할 타임프레임 키 목록 (['1d', '4h', '1h'])
        """
        total_jobs = len(tickers) * len(timeframes)
        logger.info(
            f"수집 시작: {len(tickers)} 티커 × {len(timeframes)} 타임프레임 "
            f"= {total_jobs} 작업"
        )

        pbar = tqdm(total=total_jobs, desc="전체 진행", unit="job",
                    disable=None, file=sys.stderr)

        for ticker in tickers:
            for tf_key in timeframes:
                endpoint, table, display = TIMEFRAME_CONFIG[tf_key]
                pbar.set_description(f"{ticker} {display}")

                try:
                    candles = self.fetch_candles(ticker, tf_key)
                    if candles:
                        self.save_candles(table, candles)
                        first_ts = candles[-1][1]  # 가장 오래된 캔들
                        last_ts = candles[0][1]     # 가장 최신 캔들
                        tqdm.write(
                            f"  ✓ {ticker} {display}: {len(candles):,} 캔들 "
                            f"[{first_ts} ~ {last_ts}]"
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
        print("\n" + "=" * 70)
        print("  백두코인봇 Upbit v2 Database Statistics")
        print("=" * 70)
        total_rows = 0
        for tf_key, (endpoint, table, display) in TIMEFRAME_CONFIG.items():
            try:
                cur = conn.execute(
                    f"SELECT COUNT(DISTINCT ticker), COUNT(*) FROM {table}"
                )
                tickers_cnt, rows_cnt = cur.fetchone()
                total_rows += rows_cnt
                if rows_cnt > 0:
                    cur2 = conn.execute(
                        f"SELECT MIN(timestamp), MAX(timestamp) FROM {table}"
                    )
                    min_ts, max_ts = cur2.fetchone()
                    print(f"  {display:8s}  table={table:12s}  "
                          f"tickers={tickers_cnt:3d}  rows={rows_cnt:>10,d}  "
                          f"[{min_ts} ~ {max_ts}]")
                else:
                    print(f"  {display:8s}  table={table:12s}  (비어있음)")
            except sqlite3.OperationalError as e:
                print(f"  {display:8s}  table={table:12s}  오류: {e}")
        print(f"  {'':8s}  {'TOTAL':12s}  rows={total_rows:>10,d}")
        print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="백두코인봇 Upbit OHLCV 데이터 크롤러 v2 (주요 10개 티커)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python data_crawler_v2.py                          # 전체 수집 (1d+4h+1h, 10개 티커)
  python data_crawler_v2.py --timeframe 1d           # 1일봉만
  python data_crawler_v2.py --max-pages 5            # 티커당 최대 5페이지 (1000캔들)
  python data_crawler_v2.py --stats                  # DB 통계만 출력
        """,
    )
    parser.add_argument(
        "--timeframe", "-t",
        nargs="+",
        choices=list(TIMEFRAME_CONFIG.keys()),
        default=None,
        help="수집할 타임프레임 (기본값: 1d 4h 1h 전체)",
    )
    parser.add_argument(
        "--max-pages", "-p",
        type=int,
        default=0,
        help="티커당 최대 페이지 수 (0=무제한, 1페이지=200캔들)",
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
    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="기존 DB를 삭제하고 처음부터 다시 생성",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    crawler = UpbitCrawlerV2(
        db_path=args.db,
        max_pages=args.max_pages,
        drop_existing=args.force_recreate,
    )

    try:
        if args.stats:
            crawler.stats()
            return

        # 타임프레임 결정
        timeframes = args.timeframe if args.timeframe else list(TIMEFRAME_CONFIG.keys())

        # 티커는 항상 주요 10개 고정
        tickers = MAJOR_10_TICKERS

        logger.info(f"티커: {tickers}")
        logger.info(f"타임프레임: {timeframes}")
        logger.info(f"DB: {args.db}")

        crawler.collect(tickers, timeframes)
        crawler.stats()

    finally:
        crawler.close()


if __name__ == "__main__":
    main()