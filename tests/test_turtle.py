# -*- coding: utf-8 -*-
"""터틀 전략 단위 테스트 — 진입/청산/사이징/5M 7필터."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from strategies.turtle_mtf import (FILTER_COOLDOWN_MINUTES, SLEEVE_1D, SLEEVE_5M,
                                   TradeLimiter, TurtleMTF, calc_position_size)
from tests.conftest import make_candles


# ---------------- 포지션 사이징 ----------------
def test_position_size_formula():
    """unit = equity * risk / (atr/price)"""
    size = calc_position_size(1_000_000, atr=5_000, price=100_000, risk_pct=0.01)
    assert size == pytest.approx(200_000, rel=1e-6)


def test_position_size_minimum_order():
    """계산 결과가 5,000원 미만이면 최소 주문금액으로 끌어올린다."""
    assert calc_position_size(100_000, atr=50, price=100, risk_pct=0.01) == 5_000


def test_position_size_capped_by_equity():
    assert calc_position_size(100_000, atr=1, price=1_000_000, risk_pct=0.05) <= 100_000


def test_position_size_invalid_inputs_return_zero():
    assert calc_position_size(0, 5, 100, 0.01) == 0
    assert calc_position_size(1_000_000, 0, 100, 0.01) == 0
    assert calc_position_size(1_000_000, float("nan"), 100, 0.01) == 0
    assert calc_position_size(1_000_000, 5, 0, 0.01) == 0


# ---------------- 진입 ----------------
def test_insufficient_data_returns_none():
    sig = TurtleMTF(SLEEVE_1D).check_entry(make_candles(np.full(10, 100.0)))
    assert sig.signal == "NONE"


def test_breakout_returns_long():
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 0.3, 80)
    close = np.concatenate([base, [base.max() * 1.05]])
    high = np.concatenate([base * 1.005, [base.max() * 1.06]])
    low = np.concatenate([base * 0.995, [base.max() * 1.04]])
    sig = TurtleMTF(SLEEVE_1D).check_entry(make_candles(close, high=high, low=low))
    assert sig.signal == "LONG"
    assert sig.atr > 0 and sig.dc_high > 0


def test_no_breakout_no_signal():
    close = np.concatenate([np.full(80, 100.0), [100.0]])
    sig = TurtleMTF(SLEEVE_1D).check_entry(make_candles(close))
    assert sig.signal == "NONE"
    assert "미돌파" in sig.reason


def test_live_price_can_trigger_breakout():
    """실시간가 주입(live_price)이 DB 종가보다 우선해야 한다 (DevLog 수정사항)."""
    rng = np.random.default_rng(5)
    close = 100 + rng.normal(0, 0.2, 80)
    c = make_candles(close)
    strat = TurtleMTF(SLEEVE_1D)
    assert strat.check_entry(c).signal == "NONE"
    live = float(np.max(c.high[-55:]) * 1.05)
    assert strat.check_entry(c, live_price=live).signal == "LONG"


def test_entry_ignores_intrabar_high_breakout():
    """종가 기준 돌파만 인정 — 윅만 넘는 경우는 신호 없음."""
    rng = np.random.default_rng(9)
    close = np.concatenate([100 + rng.normal(0, 0.2, 80), [100.0]])
    high = np.array(close) * 1.01
    high[-1] = float(np.max(high[:-55]) * 1.10)     # 마지막 봉 윅만 돌파
    sig = TurtleMTF(SLEEVE_1D).check_entry(make_candles(close, high=high))
    assert sig.signal == "NONE"


# ---------------- 청산 ----------------
def test_dc_low_exit():
    close = np.concatenate([np.full(30, 100.0), [80.0]])
    ex = TurtleMTF(SLEEVE_1D).check_exit(make_candles(close), entry_price=95.0, atr=1.0)
    assert ex.signal == "EXIT" and "DC20" in ex.reason


def test_atr_stop_exit():
    """DC20 Low 는 지키지만 진입가 - 2×ATR 은 깨는 구간 → ATR 손절."""
    close = np.concatenate([np.full(20, 115.0), np.tile([111.5, 109.5], 10)])
    c = make_candles(close, high=close + 0.3, low=close - 0.5)
    ex = TurtleMTF(SLEEVE_1D).check_exit(c, entry_price=120.0, atr=5.0)
    assert ex.signal == "EXIT" and "ATR" in ex.reason


def test_hold_when_no_exit_condition():
    close = np.concatenate([np.linspace(90, 100, 40), [101.0]])
    c = make_candles(close, high=close * 1.001, low=close * 0.999)
    assert TurtleMTF(SLEEVE_1D).check_exit(c, entry_price=95.0, atr=1.0).signal == "HOLD"


# ---------------- 5M 7필터 ----------------
def _uptrend_candles(n=200, tf="5m"):
    close = 100 * np.exp(np.cumsum(np.full(n, 0.002)))
    return make_candles(close, high=close * 1.003, low=close * 0.997, tf=tf)


def _breakout_5m(quality: bool = True):
    """강한 상승(ADX≥20) → 짧은 눌림(직전 종가가 채널 안) → 채널 상단 종가 돌파."""
    up = 100 * np.exp(np.cumsum(np.full(80, 0.002)))
    dip = up[-1] * np.array([0.997, 0.994])
    close_base = np.concatenate([up, dip])
    high_base = close_base * 1.003
    low_base = close_base * 0.997

    dc_high = float(np.max(high_base[-55:]))
    brk = dc_high * 1.008                      # 채널 상단을 종가로 돌파
    if quality:
        close = np.append(close_base, brk)
        high = np.append(high_base, brk * 1.004)
        low = np.append(low_base, brk * 0.992)  # 봉 범위 1.2% ≥ 1×ATR
        vol = np.concatenate([np.full(len(close_base), 10.0), [40.0]])
    else:
        close = np.append(close_base, brk)
        high = np.append(high_base, brk * 1.0001)
        low = np.append(low_base, brk * 0.9999)  # 봉 범위 0.02% < ATR
        vol = np.concatenate([np.full(len(close_base), 50.0), [1.0]])   # 평균 거래량 미달
    return make_candles(close, high=high, low=low, volume=vol, tf="5m")


def test_5m_entry_passes_all_filters():
    sig, passed, failed = TurtleMTF(SLEEVE_5M).check_entry_5m(
        _breakout_5m(), _uptrend_candles(tf="1h"), TradeLimiter())
    assert failed == [], f"실패 필터={failed}"
    assert sig.signal == "LONG"
    assert {"1H추세", "ADX", "변동성", "종가돌파", "일일제한", "돌파품질"} <= set(passed)


def test_5m_filter_1h_trend_alignment_blocks():
    down_1h = make_candles(100 * np.exp(np.cumsum(np.full(200, -0.002))), tf="1h")
    sig, _, failed = TurtleMTF(SLEEVE_5M).check_entry_5m(_breakout_5m(), down_1h, TradeLimiter())
    assert sig.signal == "NONE" and "1H추세" in failed


def test_5m_filter_breakout_quality_blocks():
    """돌파봉 범위도 작고 거래량도 평균 이하 → 돌파품질 필터 실패."""
    sig, _, failed = TurtleMTF(SLEEVE_5M).check_entry_5m(
        _breakout_5m(quality=False), _uptrend_candles(tf="1h"), TradeLimiter())
    assert sig.signal == "NONE" and "돌파품질" in failed


def test_5m_daily_trade_limit_blocks_after_three():
    limiter = TradeLimiter()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    ticker = "KRW-TEST"          # make_candles 기본 티커
    for i in range(3):
        limiter.record_entry(ticker, now + timedelta(minutes=i))
    assert limiter.can_enter(ticker, now + timedelta(minutes=10)) is False
    sig, _, failed = TurtleMTF(SLEEVE_5M).check_entry_5m(
        _breakout_5m(), _uptrend_candles(tf="1h"), limiter, now=now + timedelta(minutes=10))
    assert sig.signal == "NONE" and "일일제한" in failed


def test_stop_cooldown_60_minutes():
    limiter = TradeLimiter()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    limiter.record_stop("KRW-XRP", now)
    assert limiter.can_enter("KRW-XRP", now + timedelta(minutes=FILTER_COOLDOWN_MINUTES - 1)) is False
    assert limiter.can_enter("KRW-XRP", now + timedelta(minutes=FILTER_COOLDOWN_MINUTES + 1)) is True


def test_trade_limiter_entries_expire_after_a_day():
    limiter = TradeLimiter()
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    for _ in range(3):
        limiter.record_entry("KRW-ETH", now)
    assert limiter.can_enter("KRW-ETH", now + timedelta(days=1, minutes=1)) is True
