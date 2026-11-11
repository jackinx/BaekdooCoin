# -*- coding: utf-8 -*-
"""시장 레짐 감지기 (원본 strategies/regime_detector.py).

4분류, 우선순위 첫 매치:
  1. TRENDING   : ADX(14) > 25 AND EMA20 > EMA50
  1b. DC High 돌파 override (ADX 무시하고 TRENDING 강제)
  2. SQUEEZING  : BB Width 백분위 ≤ 20%
  3. REVERSING  : RSI ≤ 25 OR RSI ≥ 75
  4. NEUTRAL
"""
from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import indicators as ind                      # noqa: E402
from datastore import Candles                 # noqa: E402


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    SQUEEZING = "SQUEEZING"
    REVERSING = "REVERSING"
    NEUTRAL = "NEUTRAL"


class RegimeDetector:
    def __init__(self, adx_threshold: float = 25.0, bb_width_percentile: float = 0.20,
                 rsi_oversold: float = 25.0, rsi_overbought: float = 75.0,
                 ema_fast: int = 20, ema_slow: int = 50,
                 bb_period: int = 20, bb_mult: float = 2.0,
                 rsi_period: int = 14, adx_period: int = 14,
                 bbw_lookback: int = 100, adx_smoothing: str = "sma"):
        self.adx_threshold = adx_threshold
        self.bb_width_percentile = bb_width_percentile
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.bbw_lookback = bbw_lookback
        self.adx_smoothing = adx_smoothing    # "sma"(업스트림 원본, 기본) | "wilder"(TradingView 표준)

    def compute(self, c: Candles) -> dict:
        adx_v, pdi, mdi = ind.adx(c.high, c.low, c.close, self.adx_period,
                                  smoothing=self.adx_smoothing)
        upper, mid, lower = ind.bollinger(c.close, self.bb_period, self.bb_mult)
        width = ind.bb_width(upper, mid, lower)
        rsi = ind.rsi_wilder(c.close, self.rsi_period)
        ema_f = ind.ema(c.close, self.ema_fast)
        ema_s = ind.ema(c.close, self.ema_slow)

        def last(a):
            for v in a[::-1]:
                if not np.isnan(v):
                    return float(v)
            return float("nan")

        return {
            "close": float(c.close[-1]) if len(c) else float("nan"),
            "adx": last(adx_v), "plus_di": last(pdi), "minus_di": last(mdi),
            "bb_upper": last(upper), "bb_mid": last(mid), "bb_lower": last(lower),
            "bb_width": last(width),
            "bb_width_pct": ind.percentile_rank_last(width, self.bbw_lookback),
            "rsi": last(rsi),
            "ema_fast": last(ema_f), "ema_slow": last(ema_s),
        }

    def detect_regime(self, c: Candles, dc_high_override: bool = False,
                      dc_period: int = 55) -> tuple[MarketRegime, dict]:
        x = self.compute(c)
        if len(c) < 60:
            return MarketRegime.NEUTRAL, x

        ema_aligned = not (np.isnan(x["ema_fast"]) or np.isnan(x["ema_slow"])) and x["ema_fast"] > x["ema_slow"]
        if x["adx"] > self.adx_threshold and ema_aligned:
            return MarketRegime.TRENDING, x

        # 1b. 55기간 신고가 돌파는 추세로 간주 (ADX 게이트 우회)
        #     ⚠️ 현재 봉을 제외해야 성립한다. 현재 봉 high 를 포함하면 close ≤ high 이므로
        #     close > max(high) 가 수학적으로 불가능해 override 가 영원히 발동하지 않는다.
        if dc_high_override and len(c) >= dc_period + 1:
            dc_high = float(np.max(c.high[-(dc_period + 1):-1]))
            if x["close"] > dc_high:
                return MarketRegime.TRENDING, x

        if not np.isnan(x["bb_width_pct"]) and x["bb_width_pct"] <= self.bb_width_percentile:
            return MarketRegime.SQUEEZING, x

        if x["rsi"] <= self.rsi_oversold or x["rsi"] >= self.rsi_overbought:
            return MarketRegime.REVERSING, x

        return MarketRegime.NEUTRAL, x


def selfcheck() -> str:
    """자체 점검 — 실 DB가 있으면 1종 실측, 없으면 합성 데이터. (테스트에서 호출)"""
    import datastore
    db = str(Path(__file__).resolve().parent.parent / "data" / "upbit.db")
    det = RegimeDetector()
    try:
        c = datastore.load(db, "1d", "KRW-BTC")
        if len(c) >= 60:
            regime, x = det.detect_regime(c, dc_high_override=True)
            return (f"실측 KRW-BTC: {regime.value} ADX={x['adx']:.1f} RSI={x['rsi']:.1f} "
                    f"BBW_pct={x['bb_width_pct']:.2f}")
    except Exception as e:                        # noqa: BLE001
        pass
    rng = np.random.default_rng(0)
    n = 300
    close = 100 * np.exp(np.cumsum(rng.normal(0.004, 0.004, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.002, n)))
    c = Candles("TEST", "1d", [str(i) for i in range(n)], close, high, low, close,
                np.ones(n), np.ones(n))
    return f"합성 상승추세 → {det.detect_regime(c)[0].value}"


if __name__ == "__main__":
    print(selfcheck())
