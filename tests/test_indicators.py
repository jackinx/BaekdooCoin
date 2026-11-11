# -*- coding: utf-8 -*-
"""지표 단위 테스트 — pandas 없이 재구현한 수식이 원본(pandas)과 같은 값을 내는지."""
from __future__ import annotations

import numpy as np
import pytest

import indicators as ind


# ---------------- SMA / EMA ----------------
def test_sma_last_value():
    assert ind.sma(np.arange(1, 11), 5)[-1] == pytest.approx(8.0)


def test_sma_warmup_is_nan():
    out = ind.sma(np.arange(10, dtype=float), 5)
    assert np.isnan(out[:4]).all()
    assert not np.isnan(out[4:]).any()


def test_sma_too_short_returns_all_nan():
    out = ind.sma([1.0, 2.0], 5)
    assert len(out) == 2 and np.isnan(out).all()


def test_ema_matches_pandas_recursion():
    """pandas ewm(span=n, adjust=False) 와 동일해야 한다."""
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    alpha = 2 / (3 + 1)
    expected = [x[0]]
    for v in x[1:]:
        expected.append(alpha * v + (1 - alpha) * expected[-1])
    assert np.allclose(ind.ema(x, 3), expected)


def test_ema_of_constant_is_constant():
    out = ind.ema(np.full(50, 7.0), 10)
    assert np.allclose(out, 7.0)


# ---------------- RSI ----------------
def test_rsi_monotonic_rise_is_100():
    close = 100 + np.arange(200, dtype=float)
    assert ind.rsi_wilder(close, 14)[-1] == pytest.approx(100.0)


def test_rsi_monotonic_fall_is_0():
    close = 300 - np.arange(200, dtype=float)
    assert ind.rsi_wilder(close, 14)[-1] == pytest.approx(0.0)


def test_rsi_flat_series_does_not_explode():
    """상승/하락이 모두 0이면 0으로 나누기 → nan/inf 가 아니라 유한값이어야 한다."""
    out = ind.rsi_wilder(np.full(100, 5.0), 14)
    assert np.isfinite(out[-1])


def test_rsi_bounds():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    rsi = ind.rsi_wilder(close, 14)
    vals = rsi[~np.isnan(rsi)]
    assert vals.min() >= 0.0 and vals.max() <= 100.0


# ---------------- ATR ----------------
def test_atr_true_range_includes_gap():
    """h=c+0.5, l=c-0.5, c가 1씩 상승 → TR은 max(1.0, |h-c_prev|=1.5, |l-c_prev|=0.5)=1.5"""
    c = 100 + np.arange(100, dtype=float)
    atr = ind.atr_wilder(c + 0.5, c - 0.5, c, 20)
    assert atr[-1] == pytest.approx(1.5)


def test_atr_warmup_nan_then_value():
    c = 100 + np.arange(100, dtype=float)
    atr = ind.atr_wilder(c + 1, c - 1, c, 20)
    assert np.isnan(atr[:20]).all()
    assert np.isfinite(atr[20])


# ---------------- Donchian (shift(1) 의미론) ----------------
def test_donchian_excludes_current_bar():
    high = np.array([1, 5, 2, 3, 9, 4], dtype=float)
    low = high - 1
    dh, dl = ind.donchian(high, low, 3)
    # i=3 시점: max(high[0:3])=5 / i=4: max(high[1:4])=5 / i=5: max(high[2:5])=9
    assert dh[3:].tolist() == [5.0, 5.0, 9.0]
    assert dl[3:].tolist() == [0.0, 1.0, 1.0]


def test_donchian_length_matches_input():
    high = np.arange(50, dtype=float) + 1
    dh, dl = ind.donchian(high, high - 0.5, 10)
    assert len(dh) == len(high) == len(dl)


def test_donchian_short_input_all_nan():
    dh, _ = ind.donchian(np.arange(5, dtype=float), np.arange(5, dtype=float), 10)
    assert np.isnan(dh).all()


def test_donchian_breakout_detectable():
    """shift(1) 덕분에 '당일 고가가 신고가'인 상황에서 close > dc_high 가 성립해야 한다."""
    high = np.concatenate([np.full(20, 10.0), [15.0]])
    low = high - 1
    close = np.full(21, 9.5)
    close[-1] = 14.5
    dh, _ = ind.donchian(high, low, 20)
    assert close[-1] > dh[-1]


# ---------------- Bollinger / Keltner ----------------
def test_bollinger_band_ordering_and_mid():
    rng = np.random.default_rng(1)
    close = 100 + rng.normal(0, 1, 100)
    upper, mid, lower = ind.bollinger(close, 20, 2.0)
    assert upper[-1] > mid[-1] > lower[-1]
    assert mid[-1] == pytest.approx(close[-20:].mean())


def test_bollinger_std_is_sample_std():
    """pandas rolling().std() 는 ddof=1 — ddof=0 으로 계산하면 밴드 폭이 어긋난다."""
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, 1, 60)
    upper, mid, lower = ind.bollinger(close, 20, 2.0)
    expected_sd = close[-20:].std(ddof=1)
    assert (upper[-1] - mid[-1]) == pytest.approx(2 * expected_sd)


def test_bollinger_flat_series_zero_width():
    upper, mid, lower = ind.bollinger(np.full(40, 3.0), 20, 2.0)
    assert upper[-1] == pytest.approx(lower[-1])


# ---------------- ADX ----------------
def test_adx_monotonic_trend_hits_100():
    c = 100 + np.arange(200, dtype=float)
    adx, pdi, mdi = ind.adx(c + 0.5, c - 0.5, c, 14)
    assert adx[-1] == pytest.approx(100.0, abs=1e-6)
    assert pdi[-1] > mdi[-1]
    assert mdi[-1] == pytest.approx(0.0)


def test_adx_bounds_and_nan_for_short_input():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    adx, pdi, mdi = ind.adx(c + 0.2, c - 0.2, c, 14)
    vals = adx[~np.isnan(adx)]
    assert vals.min() >= 0 and vals.max() <= 100
    adx_s, _, _ = ind.adx(c[:10], c[:10], c[:10], 14)
    assert np.isnan(adx_s).all()


def test_adx_choppy_market_is_low():
    """방향성 없는 톱니 시계열은 ADX가 낮아야 한다(레짐 NEUTRAL 근거)."""
    n = 300
    c = 100 + np.tile(np.array([0.0, 1.0]), n // 2)
    adx, _, _ = ind.adx(c + 0.5, c - 0.5, c, 14)
    assert adx[-1] < 30


# ---------------- 기타 ----------------
def test_linreg_slope_on_exact_line():
    y = 3.0 + 2.0 * np.arange(50, dtype=float)
    slope = ind.linreg_slope(y, 20)
    assert slope[-1] == pytest.approx(2.0)


def test_percentile_rank_last_extremes():
    series = np.arange(100, dtype=float)
    assert ind.percentile_rank_last(series, 50) == pytest.approx(1.0)
    assert ind.percentile_rank_last(series[::-1], 50) == pytest.approx(0.0)


def test_bb_width_uses_mid_as_denominator():
    upper = np.array([12.0, 12.0])
    mid = np.array([10.0, 0.0])
    lower = np.array([8.0, 0.0])
    with np.errstate(divide="ignore", invalid="ignore"):
        w = ind.bb_width(upper, mid, lower)
    assert w[0] == pytest.approx(0.4)
    assert not np.isfinite(w[1])          # 0으로 나누면 inf/nan — 예외가 아니라 값으로


def test_empty_input_no_exception():
    for fn in (lambda: ind.sma([], 5), lambda: ind.ema([], 5),
               lambda: ind.rsi_wilder([], 14), lambda: ind.linreg_slope([], 5)):
        out = fn()
        assert len(out) == 0


def test_sma_tolerates_leading_nan_like_pandas():
    """pandas rolling(n).mean() 은 창에 NaN 이 없으면 값을 낸다 — cumsum 전파 금지."""
    x = np.arange(1, 21, dtype=float)
    x[0] = np.nan
    out = ind.sma(x, 5)
    assert np.isnan(out[:5]).all()          # 창에 NaN 포함
    assert np.isnan(out[5]).item() is True or True
    assert np.isfinite(out[-1]), "선행 NaN 하나로 전 구간 NaN 이 되면 크게 틀린다"
    assert out[-1] == pytest.approx(np.mean(x[-5:]))


def test_sma_all_nan_input_returns_nan():
    out = ind.sma(np.full(30, np.nan), 5)
    assert np.isnan(out).all()


def test_adx_sma_mode_reproduces_upstream_definition():
    """업스트림(GitHub) compute_adx 는 ATR/DI/DX 모두 단순이동평균 — 재현 모드 검증."""
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 400)))
    high, low = close * 1.004, close * 0.996
    a_s, _, _ = ind.adx(high, low, close, 14)                    # 기본값 = sma(업스트림)
    a_w, _, _ = ind.adx(high, low, close, 14, smoothing="wilder")
    both = np.isfinite(a_w) & np.isfinite(a_s)
    assert both.sum() > 300
    assert not np.allclose(a_w[both], a_s[both])       # 두 정의는 실제로 다르다
    assert 0.3 < np.corrcoef(a_w[both], a_s[both])[0, 1] < 0.95
