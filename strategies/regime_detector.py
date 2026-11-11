"""
시장 레짐 감지기 (Market Regime Detector)
========================================

ADX, BB Width, EMA 정렬을 기반으로 현재 시장 상태를 분류합니다.
이 모듈은 어떤 전략을 활성화할지 결정하는 "두뇌" 역할을 합니다.

레짐 분류:
    - TRENDING: ADX > 25 + EMA 정렬 → Turtle 추세추종
    - SQUEEZING: BB Width 최저 20% 이하 → Squeeze Momentum
    - REVERSING: RSI 25/75 극단값 → BB+RSI Reversal
    - NEUTRAL: 위 조건 중 어느 것도 해당하지 않음

참조:
    - strategy_research.md — 백두코인봇 추천 하이브리드 아키텍처
"""

import enum
import logging
from typing import Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MarketRegime(enum.Enum):
    """시장 레짐 열거형."""

    TRENDING = "TRENDING"    # 강한 추세 → Turtle
    SQUEEZING = "SQUEEZING"  # 변동성 수축 → Squeeze Momentum
    REVERSING = "REVERSING"  # 극단값 반전 → BB+RSI Reversal
    NEUTRAL = "NEUTRAL"      # 관망


class RegimeDetector:
    """시장 레짐 감지기.

    ADX, BB Width, EMA 정렬을 조합하여 현재 시장 상태를
    TRENDING / SQUEEZING / REVERSING / NEUTRAL 로 분류합니다.
    """

    def __init__(
        self,
        adx_threshold: int = 25,
        bb_width_percentile: float = 0.20,
        rsi_oversold: int = 25,
        rsi_overbought: int = 75,
        ema_fast: int = 20,
        ema_slow: int = 50,
    ):
        """
        Parameters
        ----------
        adx_threshold : int
            ADX 임계값 (기본값: 25). 이 이상이면 추세로 판단.
        bb_width_percentile : float
            BB Width 백분위 임계값 (기본값: 0.20).
            현재 BB Width가 과거의 하위 20% 이내면 Squeeze.
        rsi_oversold : int
            과매도 기준 (기본값: 25).
        rsi_overbought : int
            과매수 기준 (기본값: 75).
        ema_fast : int
            단기 EMA 기간 (기본값: 20).
        ema_slow : int
            장기 EMA 기간 (기본값: 50).
        """
        self.adx_threshold = adx_threshold
        self.bb_width_percentile = bb_width_percentile
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    @staticmethod
    def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ADX (Average Directional Index) 계산.

        Parameters
        ----------
        df : pd.DataFrame
            high, low, close 컬럼이 포함된 DataFrame.
        period : int
            ADX 계산 기간 (기본값: 14).

        Returns
        -------
        pd.Series
            ADX 값.
        """
        df = df.copy()
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # True Range
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()

        # Directional Movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)

        # Smoothed DMs
        plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

        # DX = |+DI - -DI| / (+DI + -DI) * 100
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100

        # ADX = smoothed DX
        adx = dx.rolling(window=period).mean()
        return adx

    @staticmethod
    def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
        """EMA (Exponential Moving Average) 계산.

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        period : int
            EMA 기간.

        Returns
        -------
        pd.Series
            EMA 값.
        """
        return df["close"].ewm(span=period, adjust=False).mean()

    @staticmethod
    def compute_bb_width(df: pd.DataFrame, length: int = 20, mult: float = 2.0) -> pd.Series:
        """볼린저 밴드 Width 계산.

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        length : int
            이동평균 기간.
        mult : float
            표준편차 승수.

        Returns
        -------
        pd.Series
            BB Width = (Upper - Lower) / Middle.
        """
        middle = df["close"].rolling(window=length).mean()
        std = df["close"].rolling(window=length).std()
        upper = middle + mult * std
        lower = middle - mult * std
        width = (upper - lower) / middle
        return width

    @staticmethod
    def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """RSI 계산 (Wilders Smoothing).

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        period : int
            RSI 기간.

        Returns
        -------
        pd.Series
            RSI 값.
        """
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()

        for i in range(period, len(avg_gain)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    # ------------------------------------------------------------------
    # 레짐 감지
    # ------------------------------------------------------------------
    def detect_regime(self, df: pd.DataFrame, *,
                      dc_high_override: bool = True, dc_period: int = 55) -> Tuple[MarketRegime, dict]:
        """현재 시장 레짐을 감지합니다.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame (충분한 히스토리 필요, 최소 50봉).

        Returns
        -------
        tuple(MarketRegime, dict)
            (레짐, 상세 지표 dict).
            지표 dict에는 adx, bb_width, bb_width_pct, rsi, ema_fast, ema_slow,
            ema_aligned, price_above_ema_fast, details 등이 포함됨.
        """
        if len(df) < 50:
            logger.warning("데이터 부족 (< 50봉) → NEUTRAL")
            return MarketRegime.NEUTRAL, {"reason": "데이터 부족"}

        # 지표 계산
        adx = self.compute_adx(df)
        bb_width = self.compute_bb_width(df)
        rsi = self.compute_rsi(df)
        ema_f = self.compute_ema(df, self.ema_fast)
        ema_s = self.compute_ema(df, self.ema_slow)

        current_adx = adx.iloc[-1]
        current_bb_width = bb_width.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_ema_f = ema_f.iloc[-1]
        current_ema_s = ema_s.iloc[-1]
        current_close = df["close"].iloc[-1]

        if pd.isna(current_adx) or pd.isna(current_bb_width) or pd.isna(current_rsi):
            return MarketRegime.NEUTRAL, {"reason": "지표 계산 불가 (NaN)"}

        # BB Width 백분위 계산
        bb_width_valid = bb_width.dropna()
        bb_width_pct = (
            (bb_width_valid < current_bb_width).sum() / len(bb_width_valid)
            if len(bb_width_valid) > 0 else 0.5
        )

        # EMA 정렬 확인
        ema_aligned = current_ema_f > current_ema_s  # 정배열 (단기 > 장기)
        price_above_ema_fast = current_close > current_ema_f

        # 상세 정보
        indicators = {
            "adx": round(float(current_adx), 2),
            "bb_width": round(float(current_bb_width), 4),
            "bb_width_pct": round(float(bb_width_pct), 2),
            "rsi": round(float(current_rsi), 2),
            "ema_fast": round(float(current_ema_f), 2),
            "ema_slow": round(float(current_ema_s), 2),
            "ema_aligned": bool(ema_aligned),
            "price_above_ema_fast": bool(price_above_ema_fast),
            "close": round(float(current_close), 2),
        }

        # --- 레짐 판단 ---
        # 1. TRENDING: ADX > 25 + EMA 정배열
        if current_adx > self.adx_threshold and ema_aligned:
            indicators["details"] = (
                f"추세장: ADX={current_adx:.1f} > {self.adx_threshold}, "
                f"EMA{self.ema_fast}({current_ema_f:,.0f}) > EMA{self.ema_slow}({current_ema_s:,.0f})"
            )
            return MarketRegime.TRENDING, indicators

        # 1b. DC High breakout override: Donchian Channel 55-high 돌파시 ADX 무시하고 TRENDING
        if dc_high_override and len(df) >= dc_period + 1:
            # ⚠️ 현재 봉을 채널에서 제외해야 성립한다. 현재 봉 high 를 포함하면
            #    close ≤ high 이므로 close > max(high) 가 정의상 참이 될 수 없어
            #    override 가 단 한 번도 발동하지 않는다(실측 0/24,453봉).
            dc_high = df["high"].iloc[:-1].rolling(window=dc_period).max().iloc[-1]
            if current_close > dc_high:
                indicators["details"] = (
                    f"추세장(돌파): DC{dc_period} High={dc_high:,.0f} 돌파 "
                    f"(ADX={current_adx:.1f}, EMA 정렬={ema_aligned})"
                )
                return MarketRegime.TRENDING, indicators

        # 2. SQUEEZING: BB Width 하위 20% (변동성 수축)
        if bb_width_pct <= self.bb_width_percentile:
            indicators["details"] = (
                f"변동성 수축: BB Width={current_bb_width:.4f} "
                f"(하위 {bb_width_pct*100:.1f}%)"
            )
            return MarketRegime.SQUEEZING, indicators

        # 3. REVERSING: RSI 극단값 (과매수/과매도)
        if current_rsi <= self.rsi_oversold:
            indicators["details"] = (
                f"과매도 극단값: RSI={current_rsi:.1f} ≤ {self.rsi_oversold}"
            )
            return MarketRegime.REVERSING, indicators
        if current_rsi >= self.rsi_overbought:
            indicators["details"] = (
                f"과매수 극단값: RSI={current_rsi:.1f} ≥ {self.rsi_overbought}"
            )
            return MarketRegime.REVERSING, indicators

        # 4. NEUTRAL: 기본값
        indicators["details"] = "뚜렷한 레짐 없음 (관망)"
        return MarketRegime.NEUTRAL, indicators


# ---------------------------------------------------------------------------
# 데모 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sqlite3
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    DB_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "upbit.db",
    )

    print("=" * 60)
    print("  🧠 Market Regime Detector — 데모")
    print("=" * 60)

    detector = RegimeDetector()

    # 여러 티커에 대해 레짐 감지
    tickers = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-DOGE", "KRW-ADA"]

    conn = sqlite3.connect(DB_PATH)
    for ticker in tickers:
        try:
            query = """
                SELECT timestamp, open, high, low, close, volume, volume_krw
                FROM ohlcv_1h
                WHERE ticker = ?
                ORDER BY timestamp ASC
            """
            df = pd.read_sql_query(query, conn, params=(ticker,))
            if df.empty:
                print(f"\n  {ticker}: 데이터 없음")
                continue

            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)

            regime, indicators = detector.detect_regime(df)
            emoji = {"TRENDING": "📈", "SQUEEZING": "📦", "REVERSING": "🔄", "NEUTRAL": "⏸️"}
            print(f"\n  {emoji.get(regime.value, '❓')} {ticker}: {regime.value}")
            print(f"     ADX={indicators['adx']}, RSI={indicators['rsi']}, "
                  f"BB Width={indicators['bb_width']} (pct={indicators['bb_width_pct']})")
            print(f"     EMA 정렬={indicators['ema_aligned']}, "
                  f"가격>EMA20={indicators['price_above_ema_fast']}")
            print(f"     → {indicators['details']}")
        except Exception as e:
            print(f"\n  {ticker}: ❌ {e}")

    conn.close()
    print("\n✅ Regime Detector 데모 완료")