# -*- coding: utf-8 -*-
"""레짐 감지기 단위 테스트 — 4분류 우선순위와 DC 돌파 override."""
from __future__ import annotations

import numpy as np
import pytest

import indicators as ind
from strategies.regime_detector import MarketRegime, RegimeDetector
from tests.conftest import make_candles


def _strong_uptrend(n=300):
    close = 100 * np.exp(np.cumsum(np.full(n, 0.012)))
    return make_candles(close, high=close * 1.004, low=close * 0.996)


def _squeeze_recent():
    """긴 변동성 구간 뒤 최근 20봉 변동성이 급격히 수축 → BB폭 하위 백분위."""
    rng = np.random.default_rng(7)
    noisy = 100 + rng.normal(0, 3.0, 200)
    quiet = np.full(40, noisy[-1]) + rng.normal(0, 0.02, 40)
    return make_candles(np.concatenate([noisy, quiet]))


def test_strong_uptrend_is_trending():
    det = RegimeDetector()
    regime, x = det.detect_regime(_strong_uptrend())
    assert regime is MarketRegime.TRENDING
    assert x["adx"] > 25 and x["ema_fast"] > x["ema_slow"]


def test_strong_downtrend_is_not_trending():
    """EMA 정배열 조건이 없으면 ADX가 높아도 TRENDING 이 아니다(설계 우선순위)."""
    close = 100 * np.exp(np.cumsum(np.full(300, -0.012)))
    regime, _ = RegimeDetector().detect_regime(make_candles(close))
    assert regime is not MarketRegime.TRENDING


def test_volatility_contraction_is_squeezing():
    det = RegimeDetector()
    c = _squeeze_recent()
    regime, x = det.detect_regime(c)
    assert regime is MarketRegime.SQUEEZING
    assert x["bb_width_pct"] <= 0.20


def test_oversold_extreme_is_reversing():
    det = RegimeDetector()
    prior = 100 * np.exp(np.cumsum(np.full(150, -0.002)))
    drop = prior[-1] * np.array([0.93, 0.92, 0.93, 0.92, 0.90])
    c = make_candles(np.concatenate([prior, drop]))
    regime, x = det.detect_regime(c)
    assert x["rsi"] <= 25, x["rsi"]
    assert regime is MarketRegime.REVERSING


def test_short_series_is_neutral():
    regime, _ = RegimeDetector().detect_regime(make_candles(np.full(30, 100.0)))
    assert regime is MarketRegime.NEUTRAL


def test_dc_breakout_override_forces_trending():
    """ADX가 낮아도 55기간 신고가를 돌파하면 TRENDING 으로 강제 (DevLog 수정사항)."""
    rng = np.random.default_rng(11)
    base = 100 + rng.normal(0, 1.0, 120)
    close = np.concatenate([base, [base.max() * 1.10]])
    c = make_candles(close, high=np.concatenate([base * 1.01, [base.max() * 1.11]]),
                     low=np.concatenate([base * 0.99, [base.max() * 1.08]]))
    det = RegimeDetector()
    without, x0 = det.detect_regime(c, dc_high_override=False)
    with_override, _ = det.detect_regime(c, dc_high_override=True)
    assert x0["adx"] <= 25 or without is not MarketRegime.TRENDING
    assert with_override is MarketRegime.TRENDING


def test_override_does_not_fire_without_breakout():
    """돌파가 없으면 override 플래그가 결과를 바꾸지 않는다."""
    rng = np.random.default_rng(12)
    flat = make_candles(100 + rng.normal(0, 1, 120))
    det = RegimeDetector()
    on, _ = det.detect_regime(flat, dc_high_override=True)
    off, _ = det.detect_regime(flat, dc_high_override=False)
    assert on is off


def test_priority_trending_beats_reversing():
    """강한 추세 + RSI 극단이면 TRENDING 우선 (문서화된 의도적 동작)."""
    close = 100 * np.exp(np.cumsum(np.full(400, 0.02)))
    regime, x = RegimeDetector().detect_regime(make_candles(close))
    assert regime is MarketRegime.TRENDING
    assert x["rsi"] > 75


def test_real_db_distribution_shape():
    """실측 BTC 일봉에서 레짐 분포가 문서화된 대략적 형태인지(지표 버그 조기 감지)."""
    from pathlib import Path
    import datastore
    db = Path(__file__).resolve().parent.parent / "data" / "upbit.db"
    if not db.exists():
        pytest.skip("실측 DB 없음")
    det = RegimeDetector()
    counted = {r: 0 for r in MarketRegime}
    total = 0
    for tf, ticker, step in (("1d", "KRW-BTC", 7),):
        c = datastore.load(str(db), tf, ticker)
        if len(c) < 400:
            pytest.skip("실측 데이터 부족")
        for i in range(300, len(c), step):
            sub = make_candles(c.close[:i + 1], high=c.high[:i + 1], low=c.low[:i + 1],
                               ticker=ticker, tf=tf)
            counted[det.detect_regime(sub, dc_high_override=True)[0]] += 1
            total += 1
    trend_ratio = counted[MarketRegime.TRENDING] / total
    assert 0.15 < trend_ratio < 0.65, counted
    assert counted[MarketRegime.REVERSING] / total < 0.15, counted


def test_selfcheck_runs_without_error():
    from strategies.regime_detector import selfcheck
    out = selfcheck()
    assert out and ("실측" in out or "합성" in out)


def test_compute_on_short_series_is_nan_safe():
    det = RegimeDetector()
    x = det.compute(make_candles(np.full(5, 100.0)))
    assert x["close"] == 100.0
    assert any(v != v for v in (x["adx"], x["rsi"]))       # NaN 허용, 예외 없음
