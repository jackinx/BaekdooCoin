# -*- coding: utf-8 -*-
"""데이터 저장소 단위 테스트 (SQLite)."""
from __future__ import annotations

import numpy as np
import pytest

import datastore


def _rows(ticker="KRW-BTC", n=5, start_day=1):
    return [(ticker, f"2024-01-{start_day + i:02d}T00:00:00", 100.0 + i, 101.0 + i,
             99.0 + i, 100.5 + i, 10.0 + i, (100.5 + i) * (10.0 + i)) for i in range(n)]


def test_init_db_creates_four_tables(tmp_db):
    con = datastore.connect(tmp_db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"ohlcv_5m", "ohlcv_1h", "ohlcv_4h", "ohlcv_1d"} <= names


def test_upsert_and_load_roundtrip(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows())
    c = datastore.load(tmp_db, "1d", "KRW-BTC")
    assert len(c) == 5
    assert c.ts[0] == "2024-01-01T00:00:00"
    assert c.close[-1] == pytest.approx(104.5)
    assert isinstance(c.high, np.ndarray)


def test_load_returns_ascending_order(tmp_db):
    datastore.upsert(tmp_db, "1d", list(reversed(_rows())))
    c = datastore.load(tmp_db, "1d", "KRW-BTC")
    assert c.ts == sorted(c.ts)


def test_upsert_is_idempotent_on_same_pk(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows())
    datastore.upsert(tmp_db, "1d", _rows())
    assert len(datastore.load(tmp_db, "1d", "KRW-BTC")) == 5


def test_upsert_replaces_value_on_conflict(tmp_db):
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", "2024-01-01T00:00:00", 1, 2, 0.5, 1.5, 1, 1)])
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", "2024-01-01T00:00:00", 9, 9, 9, 9.0, 9, 9)])
    c = datastore.load(tmp_db, "1d", "KRW-BTC")
    assert len(c) == 1 and c.close[0] == 9.0


def test_tickers_are_isolated(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows("KRW-BTC") + _rows("KRW-ETH"))
    assert len(datastore.load(tmp_db, "1d", "KRW-BTC")) == 5
    assert len(datastore.load(tmp_db, "1d", "KRW-ETH")) == 5


def test_last_timestamp(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows())
    assert datastore.last_timestamp(tmp_db, "1d", "KRW-BTC") == "2024-01-05T00:00:00"
    assert datastore.last_timestamp(tmp_db, "1d", "KRW-XRP") is None


def test_limit_returns_most_recent(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows())
    c = datastore.load(tmp_db, "1d", "KRW-BTC", limit=2)
    assert len(c) == 2
    assert c.ts[-1] == "2024-01-05T00:00:00"


def test_until_truncates_before_limit(tmp_db):
    """리플레이 핵심: LIMIT을 먼저 적용하면 시뮬레이션 날짜 이전 데이터가 사라진다."""
    datastore.upsert(tmp_db, "1d", _rows(n=5))
    c = datastore.load(tmp_db, "1d", "KRW-BTC", limit=2, until="2024-01-03T23:59:59")
    assert len(c) == 2
    assert c.ts[-1] == "2024-01-03T00:00:00"


def test_until_with_no_earlier_data_returns_empty(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows(start_day=10))
    assert len(datastore.load(tmp_db, "1d", "KRW-BTC", until="2024-01-05T23:59:59")) == 0


def test_empty_load_shapes(tmp_db):
    c = datastore.load(tmp_db, "1d", "KRW-NONE")
    assert len(c) == 0
    assert c.close.shape == (0,)
    assert np.isnan(c.last_close)


def test_invalid_timeframe_rejected(tmp_db):
    """SQL 인젝션/오타 방어: 화이트리스트 외 타임프레임은 즉시 거부."""
    with pytest.raises(ValueError):
        datastore.upsert(tmp_db, "1d; DROP TABLE ohlcv_1d", _rows())
    with pytest.raises(ValueError):
        datastore.load(tmp_db, "../../etc/passwd", "KRW-BTC")


def test_stats_reports_rows_and_range(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows())
    rows = datastore.stats(tmp_db)
    r = [x for x in rows if x["ticker"] == "KRW-BTC" and x["timeframe"] == "1d"][0]
    assert r["rows"] == 5
    assert r["from"] == "2024-01-01T00:00:00" and r["to"] == "2024-01-05T00:00:00"


def test_read_does_not_create_db_file(tmp_path):
    """읽기 함수가 빈 DB 파일을 만들면 이후 '데이터 있음'으로 오판한다."""
    missing = tmp_path / "nope.db"
    c = datastore.load(str(missing), "1d", "KRW-BTC")
    assert len(c) == 0
    assert datastore.stats(str(missing)) == []
    assert not missing.exists(), "읽기만 했는데 DB 파일이 생성됨"
