# -*- coding: utf-8 -*-
"""OHLCV 저장소 (SQLite). 원본 data/upbit_v2.db 와 동일 스키마.

테이블: ohlcv_5m / ohlcv_1h / ohlcv_4h / ohlcv_1d
PK (ticker, timestamp) → INSERT OR REPLACE 로 중복 방지.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import numpy as np

ALLOWED_TF = ("5m", "1h", "4h", "1d")
_TABLE = {tf: f"ohlcv_{tf}" for tf in ALLOWED_TF}

SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    ticker      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    volume_krw  REAL NOT NULL,
    PRIMARY KEY (ticker, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_{table}_ticker_ts ON {table} (ticker, timestamp);
"""


@dataclass
class Candles:
    """지표 계산용 컨테이너 (timestamp 오름차순)."""
    ticker: str
    timeframe: str
    ts: List[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    volume_krw: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def last_close(self) -> float:
        return float(self.close[-1]) if len(self.close) else float("nan")


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(db_path: str | Path) -> None:
    con = connect(db_path)
    try:
        for tf in ALLOWED_TF:
            table = _TABLE[tf]
            for stmt in SCHEMA.format(table=table).strip().split(";"):
                if stmt.strip():
                    con.execute(stmt)
        con.commit()
    finally:
        con.close()


def _table(tf: str) -> str:
    if tf not in _TABLE:
        raise ValueError(f"지원하지 않는 타임프레임: {tf}")
    return _TABLE[tf]


def upsert(db_path: str | Path, tf: str, rows: Iterable[Sequence]) -> int:
    """rows: (ticker, timestamp, open, high, low, close, volume, volume_krw)"""
    table = _table(tf)
    con = connect(db_path)
    try:
        cur = con.executemany(
            f"INSERT OR REPLACE INTO {table} "
            "(ticker,timestamp,open,high,low,close,volume,volume_krw) VALUES (?,?,?,?,?,?,?,?)",
            list(rows),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def last_timestamp(db_path: str | Path, tf: str, ticker: str) -> Optional[str]:
    table = _table(tf)
    con = connect(db_path)
    try:
        r = con.execute(f"SELECT MAX(timestamp) FROM {table} WHERE ticker=?", (ticker,)).fetchone()
        return r[0] if r and r[0] else None
    finally:
        con.close()


def load(db_path: str | Path, tf: str, ticker: str, limit: Optional[int] = None,
         until: Optional[str] = None) -> Candles:
    """until: 이 타임스탬프(포함) 이전 캔들만. limit: 최근 N개.

    ⚠️ 읽기는 DB 파일을 생성하지 않는다 — 빈 파일이 생기면 이후 '데이터 있음'으로
    오판해 조용한 실패가 난다.
    """
    if not Path(db_path).exists():
        return Candles(ticker, tf, [], *(np.array([], dtype=np.float64) for _ in range(6)))
    table = _table(tf)
    where = "ticker=?"
    params: list = [ticker]
    if until:
        where += " AND timestamp<=?"
        params.append(until)
    con = connect(db_path)
    try:
        if limit:
            rows = con.execute(
                f"SELECT timestamp,open,high,low,close,volume,volume_krw FROM {table} "
                f"WHERE {where} ORDER BY timestamp DESC LIMIT ?", (*params, limit)
            ).fetchall()[::-1]
        else:
            rows = con.execute(
                f"SELECT timestamp,open,high,low,close,volume,volume_krw FROM {table} "
                f"WHERE {where} ORDER BY timestamp ASC", params
            ).fetchall()
    finally:
        con.close()
    if not rows:
        return Candles(ticker, tf, [], *(np.array([], dtype=np.float64) for _ in range(6)))
    arr = np.array([[r[1], r[2], r[3], r[4], r[5], r[6]] for r in rows], dtype=np.float64)
    return Candles(ticker, tf, [r[0] for r in rows], arr[:, 0], arr[:, 1], arr[:, 2],
                   arr[:, 3], arr[:, 4], arr[:, 5])


def stats(db_path: str | Path) -> List[dict]:
    """DB 통계 — 파일이 없으면 빈 목록(생성하지 않음)."""
    if not Path(db_path).exists():
        return []
    out = []
    con = connect(db_path)
    try:
        for tf in ALLOWED_TF:
            table = _table(tf)
            try:
                rows = con.execute(
                    f"SELECT ticker, COUNT(*), MIN(timestamp), MAX(timestamp) FROM {table} GROUP BY ticker ORDER BY ticker"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for t, n, mn, mx in rows:
                out.append({"timeframe": tf, "ticker": t, "rows": n, "from": mn, "to": mx})
    finally:
        con.close()
    return out


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "data" / "upbit.db")
    rows = stats(db)
    if not rows:
        print(f"데이터 없음: {db}")
    for r in rows:
        print(f"{r['timeframe']:>3} {r['ticker']:<10} {r['rows']:>7}행  {r['from']} ~ {r['to']}")
