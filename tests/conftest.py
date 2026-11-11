# -*- coding: utf-8 -*-
"""pytest 공통 픽스처."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import datastore                       # noqa: E402
from datastore import Candles          # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    datastore.init_db(db)
    return db


def make_candles(close, high=None, low=None, volume=None, ticker="KRW-TEST", tf="1d",
                 start_day: str = "2024-01-01") -> Candles:
    """numpy 배열 → Candles (테스트용)."""
    close = np.asarray(close, dtype=np.float64)
    n = len(close)
    high = np.asarray(high if high is not None else close * 1.01, dtype=np.float64)
    low = np.asarray(low if low is not None else close * 0.99, dtype=np.float64)
    volume = np.asarray(volume if volume is not None else np.ones(n), dtype=np.float64)
    ts = [f"2024-01-{1 + (i % 28):02d}T00:00:00" if i < 28 else f"2024-{2 + (i // 28):02d}-{1 + (i % 28):02d}T00:00:00"
          for i in range(n)]
    # 유동성 필터(MIN_VOLUME_KRW)를 통과하도록 거래대금을 크게 둔다
    return Candles(ticker, tf, ts, close, high, low, close, volume, np.full(n, 1e11))


@pytest.fixture()
def candles_factory():
    return make_candles


@pytest.fixture()
def trending_candles():
    """55봉 횡보 후 강한 돌파 (터틀 진입이 발생해야 하는 시계열)."""
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 0.4, 80)
    base = np.concatenate([base, 100 + np.arange(1, 21) * 2.0])   # 마지막 20봉 상승
    return make_candles(base)
