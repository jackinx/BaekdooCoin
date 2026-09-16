#!/usr/bin/env python3
"""
백두코인봇 Upbit 데이터 업데이터 (일일 실행용)
==========================================
주요 10개 KRW 마켓의 최신 OHLCV 캔들만 가져와 upbit_v2.db에 추가/갱신합니다.
이미 수집된 데이터를 다시 다운로드하지 않고, DB에 없는 최신 캔들만 추가합니다.

사용법:
    # 수동 실행 (전체 타임프레임 업데이트)
    python data_updater.py

    # 특정 타임프레임만
    python data_updater.py --timeframe 1d 4h

    # 통계만 확인
    python data_updater.py --stats

크론탭 예시 (매일):
    # 매일 오전 9시 15분 (한국장 시작 전) 1일봉 업데이트
    15 9 * * * cd /Volumes/X31/workspace/백두코인봇_Upbit && /usr/bin/python3 data_updater.py --timeframe 1d >> logs/updater.log 2>&1

    # 4시간마다 4h, 1h 업데이트
    0 */4 * * * cd /Volumes/X31/workspace/백두코인봇_Upbit && /usr/bin/python3 data_updater.py --timeframe 4h 1h >> logs/updater_4h.log 2>&1

전략:
    - 각 티커/타임프레임별로 DB의 가장 최근 timestamp를 조회
    - 해당 timestamp 이후의 캔들만 API로 가져옴 (최대 200개)
    - INSERT OR REPLACE로 중복 없이 저장
    - 새로 추가된 캔들 수를 보고
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

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "upbit_v2.db")
API_BASE = "https://api.upbit.com"
RATE_LIMIT_SLEEP = 0.15
MAX_CANDLES_PER_CALL = 200

# 주요 10개 KRW 마켓 티커
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

TIMEFRAME_CONFIG = {
    "5m": ("minutes/5",   "ohlcv_5m",  "5분봉"),
    "1h": ("minutes/60",  "ohlcv_1h",  "1시간봉"),
    "4h": ("minutes/240", "ohlcv_4h",  "4시간봉"),
    "1d": ("days",        "ohlcv_1d",  "1일봉"),
}

INSERT_SQL = """\
INSERT OR REPLACE INTO {table}
    (ticker, timestamp, open, high, low, close, volume, volume_krw)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?);
"""

# 로깅
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upbit_updater")


# ---------------------------------------------------------------------------
# Upbit 데이터 업데이터
# ---------------------------------------------------------------------------
class UpbitDataUpdater:
    """업비트 OHLCV 데이터 일일 업데이트 클래스."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "BaekdooCoin-Upbit-Updater/2.0",
        })
        self.stats = {}  # 테이블별 추가 통계

    # ------------------------------------------------------------------
    # 데이터베이스 연결
    # ------------------------------------------------------------------
    def _ensure_conn(self) -> sqlite3.Connection:
        if self.conn is None:
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(
                    f"DB 파일이 없습니다: {self.db_path}\n"
                    f"먼저 data_crawler_v2.py를 실행하여 초기 데이터를 수집하세요."
                )
            self.conn = sqlite3.connect(self.db_path)
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ------------------------------------------------------------------
    # API 호출
    # ------------------------------------------------------------------
    def _api_get(self, endpoint: str, params: dict = None,
                 max_retries: int = 5) -> list:
        """Upbit API GET 호출 (재시도 포함)."""
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
                        f"API 호출 최대 재시도 초과: {url}"
                    ) from last_exc

        raise RuntimeError(f"API unreachable: {url}") from last_exc

    # ------------------------------------------------------------------
    # 특정 티커의 가장 최근 timestamp 조회
    # ------------------------------------------------------------------
    def _get_latest_ts(self, table: str, ticker: str) -> Optional[str]:
        """DB에서 해당 티커의 가장 최근 timestamp 반환."""
        conn = self._ensure_conn()
        cur = conn.execute(
            f"SELECT MAX(timestamp) FROM {table} WHERE ticker = ?",
            (ticker,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # 최신 캔들만 가져오기
    # ------------------------------------------------------------------
    def fetch_latest_candles(
        self,
        market: str,
        timeframe: str,
    ) -> list[tuple]:
        """
        DB에 마지막으로 저장된 시점 이후의 최신 캔들만 가져옵니다.
        DB에 기존 데이터가 없으면 최신 200개를 가져옵니다.

        Returns:
            [(ticker, timestamp, open, high, low, close, volume, volume_krw), ...]
        """
        endpoint_template, table_name, display_name = TIMEFRAME_CONFIG[timeframe]
        latest_ts = self._get_latest_ts(table_name, market)

        params = {
            "market": market,
            "count": MAX_CANDLES_PER_CALL,
        }

        # DB에 데이터가 있으면 해당 시점 이후부터 가져옴
        # Upbit API는 'to' 파라미터가 있으면 그 시점 이전 데이터를 반환하므로
        # 최신 데이터를 가져오려면 to를 지정하지 않음
        # 대신, 가져온 데이터 중에서 DB에 없는 것만 필터링
        raw = self._api_get(f"candles/{endpoint_template}", params=params)
        if not raw:
            return []

        new_candles = []
        for candle in raw:
            ts = candle.get("candle_date_time_utc", "")

            # DB 최신 시점보다 이후인 데이터만 추가
            if latest_ts is None or ts > latest_ts:
                new_candles.append((
                    market,
                    ts,
                    float(candle["opening_price"]),
                    float(candle["high_price"]),
                    float(candle["low_price"]),
                    float(candle["trade_price"]),
                    float(candle["candle_acc_trade_volume"]),
                    float(candle["candle_acc_trade_price"]),
                ))

        return new_candles

    # ------------------------------------------------------------------
    # 특정 티커의 가장 오래된 timestamp 조회
    # ------------------------------------------------------------------
    def _get_earliest_ts(self, table: str, ticker: str) -> Optional[str]:
        """DB에서 해당 티커의 가장 오래된 timestamp 반환."""
        conn = self._ensure_conn()
        cur = conn.execute(
            f"SELECT MIN(timestamp) FROM {table} WHERE ticker = ?",
            (ticker,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # 과거 데이터 역방향 수집 (백필)
    # ------------------------------------------------------------------
    def backfill(
        self,
        tickers: list[str],
        timeframe: str,
        max_requests_per_ticker: int = 500,
    ) -> dict:
        """
        각 티커의 가장 오래된 데이터 시점보다 더 과거 데이터를
        Upbit API의 'to' 파라미터를 이용해 역방향으로 가져옵니다.
        API가 빈 응답을 반환하거나 충분히 과거까지 도달하면 중단.

        max_requests_per_ticker: 티커당 최대 API 호출 횟수 (200개/호출)
        """
        endpoint_template, table_name, display_name = TIMEFRAME_CONFIG[timeframe]
        table_stats = {"ticker_stats": {}, "total_added": 0}

        for ticker in tickers:
            earliest_ts = self._get_earliest_ts(table_name, ticker)

            if earliest_ts is None:
                # 데이터가 아예 없으면 최신 200개 먼저 가져옴
                logger.info(f"  {ticker}: 데이터 없음, 최신 200개 먼저 수집...")
                params = {"market": ticker, "count": MAX_CANDLES_PER_CALL}
                raw = self._api_get(f"candles/{endpoint_template}", params=params)
                candles = self._parse_candles(raw)
                if candles:
                    self._save_candles(table_name, candles)
                    table_stats["total_added"] += len(candles)
                    table_stats["ticker_stats"][ticker] = {"added": len(candles)}
                    earliest_ts = candles[-1][1]  # timestamp of oldest candle
                    logger.info(f"  ✓ {ticker} {display_name}: 최초 {len(candles)}개 수집")
                else:
                    logger.warning(f"  ✗ {ticker}: API 데이터 없음")
                    continue

            # 'to' 파라미터로 역방향 수집
            # earliest_ts는 '2026-08-22T07:30:00' 형식의 ISO UTC
            to_param = earliest_ts
            ticker_added = 0
            ticker_requests = 0
            stop_backfill = False

            for req_num in range(1, max_requests_per_ticker + 1):
                if stop_backfill:
                    break

                params = {
                    "market": ticker,
                    "count": MAX_CANDLES_PER_CALL,
                    "to": to_param,
                }

                raw = self._api_get(f"candles/{endpoint_template}", params=params)
                time.sleep(RATE_LIMIT_SLEEP)
                ticker_requests += 1

                if not raw or len(raw) == 0:
                    logger.info(f"  ✓ {ticker} {display_name}: API 데이터 끝에 도달 (req #{req_num})")
                    break

                candles = self._parse_candles(raw)
                if not candles:
                    logger.info(f"  ✓ {ticker} {display_name}: 더 이상 새 데이터 없음")
                    break

                # 중복 방지: 이미 DB에 있는 timestamp는 제외
                existing_ts = set()
                conn = self._ensure_conn()
                cur = conn.execute(
                    f"SELECT timestamp FROM {table_name} WHERE ticker = ?",
                    (ticker,)
                )
                for row in cur.fetchall():
                    existing_ts.add(row[0])

                new_rows = [r for r in candles if r[1] not in existing_ts]
                if not new_rows:
                    logger.info(f"  ✓ {ticker} {display_name}: 기존 데이터와 전부 중복 (req #{req_num})")
                    break

                self._save_candles(table_name, new_rows)
                ticker_added += len(new_rows)
                table_stats["total_added"] += len(new_rows)

                # 다음 배치를 위한 가장 오래된 timestamp
                oldest_in_batch = min(r[1] for r in new_rows)

                logger.info(
                    f"  ✓ {ticker} {display_name}: req#{req_num} → "
                    f"{len(new_rows)}개 추가 (∼{oldest_in_batch})"
                )

                # 응답이 200개 미만이면 끝까지 도달한 것
                if len(raw) < MAX_CANDLES_PER_CALL:
                    logger.info(
                        f"  ✓ {ticker} {display_name}: 마지막 페이지 도달 (req #{req_num}, "
                        f"{len(raw)}개)"
                    )
                    break

                to_param = oldest_in_batch

            logger.info(
                f"  {ticker} {display_name}: 총 {ticker_added}개 추가 "
                f"({ticker_requests}회 호출)"
            )
            table_stats["ticker_stats"][ticker] = {
                "added": ticker_added,
                "requests": ticker_requests,
            }

        self.stats[table_name] = {
            "added": table_stats["total_added"],
            "tickers": [t for t in tickers],
            "errors": [],
        }
        return self.stats

    def _parse_candles(self, raw: list[dict]) -> list[tuple]:
        """API 응답을 INSERT용 튜플 리스트로 변환."""
        rows = []
        for candle in raw:
            ts = candle.get("candle_date_time_utc", "")
            rows.append((
                candle.get("market", ""),
                ts,
                float(candle["opening_price"]),
                float(candle["high_price"]),
                float(candle["low_price"]),
                float(candle["trade_price"]),
                float(candle["candle_acc_trade_volume"]),
                float(candle["candle_acc_trade_price"]),
            ))
        return rows

    # ------------------------------------------------------------------
    # 캔들 저장
    # ------------------------------------------------------------------
    def _save_candles(self, table: str, rows: list[tuple]):
        """캔들 데이터를 DB에 저장."""
        if not rows:
            return
        conn = self._ensure_conn()
        sql = INSERT_SQL.format(table=table)
        conn.executemany(sql, rows)
        conn.commit()

    # ------------------------------------------------------------------
    # 업데이트 실행
    # ------------------------------------------------------------------
    def update(
        self,
        tickers: list[str],
        timeframes: list[str],
    ) -> dict:
        """
        모든 티커 × 타임프레임의 최신 캔들을 업데이트합니다.

        Returns:
            {table_name: {"added": N, "tickers": [str, ...]}, ...}
        """
        stats = {}

        for tf_key in timeframes:
            endpoint, table, display = TIMEFRAME_CONFIG[tf_key]
            table_stats = {"added": 0, "tickers": [], "errors": []}
            stats[table] = table_stats

            for ticker in tickers:
                try:
                    new_candles = self.fetch_latest_candles(ticker, tf_key)
                    if new_candles:
                        self._save_candles(table, new_candles)
                        table_stats["added"] += len(new_candles)
                        table_stats["tickers"].append(ticker)
                        logger.info(
                            f"  ✓ {ticker} {display}: {len(new_candles)}개 "
                            f"신규 캔들 추가"
                        )
                    else:
                        logger.debug(f"  - {ticker} {display}: 신규 데이터 없음")
                except Exception as e:
                    logger.error(f"  ✗ {ticker} {display}: {e}")
                    table_stats["errors"].append(str(e))

                time.sleep(RATE_LIMIT_SLEEP)

        self.stats = stats
        return stats

    # ------------------------------------------------------------------
    # 업데이트 결과 보고
    # ------------------------------------------------------------------
    def report(self):
        """업데이트 결과 요약 출력."""
        print("\n" + "=" * 60)
        print("  백두코인봇 Upbit v2 — 업데이트 결과")
        print("=" * 60)

        total_added = 0
        for table, info in self.stats.items():
            added = info.get("added", 0)
            errors = info.get("errors", [])
            total_added += added

            if added > 0:
                ticker_list = info.get("tickers", [])
                print(f"  {table}: {added}개 캔들 추가 "
                      f"({len(ticker_list)} 티커)")
            else:
                print(f"  {table}: 신규 데이터 없음 (최신 상태)")

            if errors:
                print(f"    ⚠ 오류: {len(errors)}건")

        print(f"  총 {total_added}개 캔들 추가됨")
        print("=" * 60)

        # 전체 DB 통계
        self._db_stats()

    # ------------------------------------------------------------------
    # 전체 통계
    # ------------------------------------------------------------------
    def _db_stats(self):
        """DB 전체 통계 출력."""
        conn = self._ensure_conn()
        print("\n  현재 DB 상태:")
        for tf_key, (endpoint, table, display) in TIMEFRAME_CONFIG.items():
            try:
                cur = conn.execute(
                    f"SELECT COUNT(DISTINCT ticker), COUNT(*) FROM {table}"
                )
                tickers_cnt, rows_cnt = cur.fetchone()
                if rows_cnt > 0:
                    cur2 = conn.execute(
                        f"SELECT MIN(timestamp), MAX(timestamp) FROM {table}"
                    )
                    min_ts, max_ts = cur2.fetchone()
                    print(f"    {display:8s}  tickers={tickers_cnt}  "
                          f"rows={rows_cnt:>10,d}  [{min_ts} ~ {max_ts}]")
                else:
                    print(f"    {display:8s}  (비어있음)")
            except sqlite3.OperationalError:
                pass
        print()


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="백두코인봇 Upbit 데이터 업데이터 (일일/주기적 실행용)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python data_updater.py                           # 전체 업데이트
  python data_updater.py --timeframe 1d            # 1일봉만 업데이트
  python data_updater.py --timeframe 5m --backfill # 5분봉 역방향 수집
  python data_updater.py --stats                   # DB 통계만 확인
        """,
    )
    parser.add_argument(
        "--timeframe", "-t",
        nargs="+",
        choices=list(TIMEFRAME_CONFIG.keys()),
        default=None,
        help="업데이트할 타임프레임 (기본값: 전체)",
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
        "--backfill", "-b",
        nargs="*",
        const=None,
        help="과거 데이터 역방향 수집 (max_requests_per_ticker 기본 500, 지정 가능)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    updater = UpbitDataUpdater(db_path=args.db)

    try:
        if args.stats:
            updater._db_stats()
            return

        timeframes = args.timeframe if args.timeframe else list(TIMEFRAME_CONFIG.keys())
        tickers = MAJOR_10_TICKERS

        # 백필 모드: 과거 데이터 역방향 수집
        if args.backfill is not None:
            logger.info(f"백필 시작: {len(tickers)} 티커 × {len(timeframes)} 타임프레임")
            logger.info(f"DB: {args.db}")

            # max_requests_per_ticker 기본값 500, 인자가 있으면 첫번째 인자 사용
            max_req = 500
            if args.backfill and len(args.backfill) > 0:
                try:
                    max_req = int(args.backfill[0])
                except ValueError:
                    pass

            for tf_key in timeframes:
                updater.backfill(tickers, tf_key, max_requests_per_ticker=max_req)

            updater.report()
            return

        logger.info(f"업데이트 시작: {len(tickers)} 티커 × {len(timeframes)} 타임프레임")
        logger.info(f"DB: {args.db}")

        updater.update(tickers, timeframes)
        updater.report()

    finally:
        updater.close()


if __name__ == "__main__":
    main()