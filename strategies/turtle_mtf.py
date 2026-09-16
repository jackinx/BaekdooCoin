"""
백두코인봇 — Multi-TF Turtle Strategy (5M + 1D Dual Sleeve)
===========================================================

1D 터틀 (원본) + 5M 터틀 (신규) 듀얼 슬리브, Graduation 승급 시스템 포함.

슬리브 구성:
  - 1D Sleeve: 55일 Donchian, 1.0% 리스크/건, 피라미딩 2유닛, 동시 5종목, 6% 슬리브캡
  - 5M Sleeve: 55봉 Donchian, 0.25~0.35% 리스크/건, 피라미딩 없음(v1), 동시 3종목, 1.5% 슬리브캡
  - 포트폴리오 총캡: ≤ 7%

7종 필터 (5M):
  1. 1H 추세정렬: close > EMA50(1H) & EMA20 > EMA50(1H)
  2. ADX 게이트: ADX(14, 5M) ≥ 20 & +DI > -DI
  3. 레짐 게이트: 1H TRENDING만 허용
  4. 변동성 하한: ATR_5M/Price ≥ 0.05%
  5. 종가 돌파만 (윅 돌파 무시)
  6. 일일 제한: 종목당 최대 3회, 손절 후 1시간 쿨다운
  7. 돌파 품질: 돌파봉 범위 ≥ 1.0×ATR_5M 또는 거래량 ≥ 20봉 평균

Graduation (승급):
  5M 포지션이 +1.5×ATR_5m 수익 중일 때, 1D 돌파 신호 발생 시 → 1D로 승급
  조건: 1H 추세 유지 & 종목당 리스크 ≤ 2.5%
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MTFFilterResult:
    """멀티TF 필터링 결과."""
    passed: bool
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class MTFSignal:
    """멀티TF 신호."""
    sleeve: str           # "1D" | "5M"
    signal: str           # "LONG" | "EXIT" | "NONE"
    ticker: str
    price: float
    atr: float
    reason: str
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Multi-TF Turtle Strategy
# ---------------------------------------------------------------------------

class TurtleMTFStrategy:
    """멀티TF 터틀 트레이딩 시스템.

    1D + 5M 듀얼 슬리브, 7종 필터, Graduation(승급) 통합.
    """

    # ── Sleeve parameters ──
    SLEEVE_1D = {
        "donchian_period": 55,
        "exit_period": 20,
        "risk_pct": 0.01,          # 1.0% per entry
        "atr_mult": 2.0,
        "max_pyramid": 2,          # max 2 units
        "max_stocks": 5,           # concurrent max
        "sleeve_risk_cap": 0.06,   # 6%
        "timeframe": "1d",
        "table": "ohlcv_1d",
    }
    SLEEVE_5M = {
        "donchian_period": 55,
        "exit_period": 20,
        "risk_pct": 0.003,         # 0.30% per entry (0.25~0.35%)
        "atr_mult": 2.0,
        "max_pyramid": 1,          # no pyramid in v1
        "max_stocks": 3,           # concurrent max
        "sleeve_risk_cap": 0.015,  # 1.5%
        "timeframe": "5m",
        "table": "ohlcv_5m",
    }
    PORTFOLIO_TOTAL_CAP = 0.07  # 7%

    # ── 5M Filter parameters ──
    FILTER_ADX_MIN = 20
    FILTER_VOLATILITY_MIN_PCT = 0.05
    FILTER_BAR_RANGE_ATR_MULT = 1.0
    FILTER_VOLUME_AVG_MULT = 1.0
    FILTER_DAILY_MAX_TRADES = 3
    FILTER_COOLDOWN_MINUTES = 60

    # ── Graduation parameters ──
    GRAD_PROFIT_ATR_MULT = 1.5
    GRAD_MAX_RISK_PCT = 0.025

    def __init__(self, db_path: str, ticker: str):
        self.db_path = db_path
        self.ticker = ticker

        # Daily trade tracking per ticker (5M sleeve)
        self._daily_trades: Dict[str, int] = {}
        self._daily_date: str = ""
        self._last_stop_date: Optional[datetime] = None  # cooldown after stop-loss

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    # ── Max bars to load per table (warmup: Donchian 55 + EMA 50 + ADX 14) ──
    _LOAD_LIMITS = {"ohlcv_1d": 200, "ohlcv_1h": 300, "ohlcv_5m": 500}

    def _load_ohlcv(self, table: str) -> pd.DataFrame:
        """Load OHLCV data from DB — last N bars only (cached in memory)."""
        import sqlite3
        limit_rows = self._LOAD_LIMITS.get(table, 200)
        conn = sqlite3.connect(self.db_path)
        query = f"""
            SELECT * FROM (
                SELECT timestamp, open, high, low, close, volume, volume_krw
                FROM {table}
                WHERE ticker = ?
                ORDER BY timestamp DESC
                LIMIT {limit_rows}
            )
            ORDER BY timestamp ASC
        """
        df = pd.read_sql_query(query, conn, params=(self.ticker,))
        conn.close()
        if df.empty:
            raise ValueError(f"No data: ticker={self.ticker}, table={table}")
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # ------------------------------------------------------------------
    # Indicators (static — shared by both sleeves)
    # ------------------------------------------------------------------
    @staticmethod
    def atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    @staticmethod
    def donchian(df: pd.DataFrame, period: int) -> pd.DataFrame:
        df = df.copy()
        df["dc_high"] = df["high"].rolling(window=period).max().shift(1)  # exclude current
        df["dc_low"] = df["low"].rolling(window=period).min().shift(1)
        df["dc_mid"] = (df["dc_high"] + df["dc_low"]) / 2
        return df

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def adx_series(df: pd.DataFrame, period: int = 14) -> tuple:
        """Returns (ADX, +DI, -DI)."""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_val = tr.rolling(window=period).mean()

        up = high.diff()
        dn = -low.diff()
        plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)

        plus_di = 100 * (plus_dm.rolling(period).mean() / atr_val)
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr_val)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        adx_val = dx.rolling(period).mean()
        return adx_val, plus_di, minus_di

    # ------------------------------------------------------------------
    # Common entry/exit (parameterized)
    # ------------------------------------------------------------------
    def check_entry_sleeve(self, df: pd.DataFrame, sleeve: dict) -> MTFSignal:
        """Generic sleeve entry check."""
        period = sleeve["donchian_period"]
        if len(df) < period + 5:
            return MTFSignal(sleeve="?", signal="NONE", ticker=self.ticker,
                            price=0, atr=0, reason=f"Not enough bars (< {period + 5})")

        df = self.donchian(df, period)
        atr_series = self.atr(df, 20)

        prev_close = df["close"].iloc[-2]
        prev_high = df["dc_high"].iloc[-2]
        curr_close = df["close"].iloc[-1]
        curr_high = df["dc_high"].iloc[-1]
        curr_atr = float(atr_series.iloc[-1])

        if pd.isna(curr_high) or pd.isna(curr_atr) or curr_atr <= 0:
            return MTFSignal(sleeve="?", signal="NONE", ticker=self.ticker,
                            price=curr_close, atr=0, reason="NaN indicator")

        if prev_close <= prev_high and curr_close > curr_high:
            return MTFSignal(
                sleeve=sleeve["timeframe"].upper(),
                signal="LONG", ticker=self.ticker,
                price=float(curr_close), atr=curr_atr,
                reason=f"{period}-bar Donchian breakout (dc={curr_high:,.0f}, close={curr_close:,.0f})",
            )

        return MTFSignal(
            sleeve=sleeve["timeframe"].upper(), signal="NONE", ticker=self.ticker,
            price=float(curr_close), atr=curr_atr,
            reason=f"No {period}-bar breakout",
        )

    def check_exit_sleeve(self, df: pd.DataFrame, sleeve: dict,
                          entry_price: float) -> MTFSignal:
        """Generic sleeve exit check."""
        exit_period = sleeve["exit_period"]
        atr_mult = sleeve["atr_mult"]

        if len(df) < exit_period + 1:
            return MTFSignal(sleeve="?", signal="EXIT", ticker=self.ticker,
                            price=0, atr=0, reason="Data shortage")

        df_dc = self.donchian(df, exit_period)
        atr_series = self.atr(df, 20)

        curr_close = float(df["close"].iloc[-1])
        curr_low = float(df_dc["dc_low"].iloc[-1])
        curr_atr = float(atr_series.iloc[-1])

        if not pd.isna(curr_low) and curr_close < curr_low:
            return MTFSignal(
                sleeve=sleeve["timeframe"].upper(), signal="EXIT",
                ticker=self.ticker, price=curr_close, atr=curr_atr,
                reason=f"{exit_period}-bar low break (dc_low={curr_low:,.0f})",
            )

        stop = entry_price - atr_mult * curr_atr
        if curr_close <= stop:
            return MTFSignal(
                sleeve=sleeve["timeframe"].upper(), signal="EXIT",
                ticker=self.ticker, price=curr_close, atr=curr_atr,
                reason=f"{atr_mult}×ATR stop (entry={entry_price:,.0f})",
            )

        return MTFSignal(
            sleeve=sleeve["timeframe"].upper(), signal="HOLD",
            ticker=self.ticker, price=curr_close, atr=curr_atr,
            reason="No exit condition",
        )

    # ------------------------------------------------------------------
    # 5M 필터 #1: 1H Trend Alignment
    # ------------------------------------------------------------------
    def _filter_trend_alignment(self, df_1h: pd.DataFrame) -> MTFFilterResult:
        if len(df_1h) < 50:
            return MTFFilterResult(False, "1H data < 50 bars")
        close = df_1h["close"]
        ema20 = self.ema(close, 20)
        ema50 = self.ema(close, 50)

        curr_close = float(close.iloc[-1])
        curr_ema20 = float(ema20.iloc[-1])
        curr_ema50 = float(ema50.iloc[-1])

        if pd.isna(curr_ema20) or pd.isna(curr_ema50):
            return MTFFilterResult(False, "EMA NaN")

        close_above = curr_close > curr_ema50
        ema_aligned = curr_ema20 > curr_ema50

        if close_above and ema_aligned:
            return MTFFilterResult(True, "Trend aligned",
                                  {"ema20": curr_ema20, "ema50": curr_ema50, "close": curr_close})
        return MTFFilterResult(False,
            f"Trend NOT aligned: close>EMA50={close_above}, EMA20>EMA50={ema_aligned}")

    # ------------------------------------------------------------------
    # 5M 필터 #2: ADX Gate
    # ------------------------------------------------------------------
    def _filter_adx_gate(self, df_5m: pd.DataFrame) -> MTFFilterResult:
        if len(df_5m) < 20:
            return MTFFilterResult(False, "5M data < 20 bars")
        adx_val, plus_di, minus_di = self.adx_series(df_5m, 14)
        curr_adx = float(adx_val.iloc[-1])
        curr_pdi = float(plus_di.iloc[-1])
        curr_mdi = float(minus_di.iloc[-1])

        if pd.isna(curr_adx):
            return MTFFilterResult(False, "ADX NaN")

        if curr_adx >= self.FILTER_ADX_MIN and curr_pdi > curr_mdi:
            return MTFFilterResult(True, "ADX gate passed",
                                  {"adx": curr_adx, "+DI": curr_pdi, "-DI": curr_mdi})
        return MTFFilterResult(False,
            f"ADX={curr_adx:.1f}<{self.FILTER_ADX_MIN} or +DI({curr_pdi:.1f})<=-DI({curr_mdi:.1f})")

    # ------------------------------------------------------------------
    # 5M 필터 #4: Volatility floor
    # ------------------------------------------------------------------
    def _filter_volatility(self, df_5m: pd.DataFrame) -> MTFFilterResult:
        atr_series = self.atr(df_5m, 20)
        curr_atr = float(atr_series.iloc[-1])
        curr_close = float(df_5m["close"].iloc[-1])

        if pd.isna(curr_atr) or curr_atr <= 0 or curr_close <= 0:
            return MTFFilterResult(False, "Volatility NaN/zero")

        vol_pct = curr_atr / curr_close * 100
        if vol_pct >= self.FILTER_VOLATILITY_MIN_PCT:
            return MTFFilterResult(True, f"Volatility OK: {vol_pct:.3f}%")
        return MTFFilterResult(False,
            f"Volatility too low: {vol_pct:.3f}% < {self.FILTER_VOLATILITY_MIN_PCT}%")

    # ------------------------------------------------------------------
    # 5M 필터 #5: Close-only breakout (no wick breakout)
    # ------------------------------------------------------------------
    def _filter_close_breakout(self, df_5m: pd.DataFrame, period: int = 55) -> MTFFilterResult:
        """Verify that the breakout was by CLOSE, not wick."""
        if len(df_5m) < period + 1:
            return MTFFilterResult(False, "Not enough bars")
        df_dc = self.donchian(df_5m, period)
        curr_close = float(df_5m["close"].iloc[-1])
        curr_high = float(df_dc["dc_high"].iloc[-1])  # shifted(1) already

        if pd.isna(curr_high):
            return MTFFilterResult(False, "Donchian NaN")

        # This is the standard check — implied by check_entry_sleeve using dc_high.shift(1)
        # We just confirm close > dc_high here
        if curr_close > curr_high:
            return MTFFilterResult(True, "Close breakout confirmed")
        return MTFFilterResult(False, "Not a close breakout")

    # ------------------------------------------------------------------
    # 5M 필터 #6: Daily trade limit + cooldown
    # ------------------------------------------------------------------
    def _filter_daily_limit(self) -> MTFFilterResult:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Reset daily counter
        if today != self._daily_date:
            self._daily_trades = {}
            self._daily_date = today

        count = self._daily_trades.get(self.ticker, 0)
        if count >= self.FILTER_DAILY_MAX_TRADES:
            return MTFFilterResult(False,
                f"Daily limit: {count}/{self.FILTER_DAILY_MAX_TRADES} trades reached")
        return MTFFilterResult(True, f"Daily count: {count}/{self.FILTER_DAILY_MAX_TRADES}")

    def _filter_cooldown(self) -> MTFFilterResult:
        """Check if stop-loss cooldown is active."""
        if self._last_stop_date is None:
            return MTFFilterResult(True, "No cooldown")
        now = datetime.now(timezone.utc)
        elapsed = (now - self._last_stop_date).total_seconds() / 60
        if elapsed >= self.FILTER_COOLDOWN_MINUTES:
            self._last_stop_date = None
            return MTFFilterResult(True, "Cooldown passed")
        return MTFFilterResult(False,
            f"Cooldown: {elapsed:.0f}/{self.FILTER_COOLDOWN_MINUTES}min remaining")

    def record_trade(self, trade_type: str):
        """Record a trade (entry or exit) for daily limit tracking."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_trades = {}
            self._daily_date = today
        self._daily_trades[self.ticker] = self._daily_trades.get(self.ticker, 0) + 1
        if trade_type == "STOP_LOSS":
            self._last_stop_date = now

    # ------------------------------------------------------------------
    # 5M 필터 #7: Bar quality
    # ------------------------------------------------------------------
    def _filter_bar_quality(self, df_5m: pd.DataFrame) -> MTFFilterResult:
        """Breakout bar range >= 1.0×ATR OR volume >= 20-bar average."""
        atr_series = self.atr(df_5m, 20)
        curr_atr = float(atr_series.iloc[-1])
        bar_range = float(df_5m["high"].iloc[-1] - df_5m["low"].iloc[-1])
        bar_volume = float(df_5m["volume"].iloc[-1])
        avg_vol = float(df_5m["volume"].iloc[-21:-1].mean())

        if pd.isna(curr_atr) or pd.isna(avg_vol):
            return MTFFilterResult(True, "Insufficient data for bar quality — pass")

        range_ok = bar_range >= self.FILTER_BAR_RANGE_ATR_MULT * curr_atr
        vol_ok = bar_volume >= self.FILTER_VOLUME_AVG_MULT * avg_vol

        if range_ok or vol_ok:
            return MTFFilterResult(True,
                f"Bar quality OK: range={bar_range/curr_atr:.2f}×ATR, vol={bar_volume/avg_vol:.2f}×avg")
        return MTFFilterResult(False,
            f"Weak bar: range={bar_range/curr_atr:.2f}×ATR, vol={bar_volume/avg_vol:.2f}×avg")

    # ------------------------------------------------------------------
    # 5M Combined filter pipeline
    # ------------------------------------------------------------------
    def run_5m_filters(self, df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> MTFFilterResult:
        """Run all 7 filters for 5M entry. Returns first failure or pass."""
        filters = [
            ("1H Trend Alignment", self._filter_trend_alignment(df_1h)),
            ("ADX Gate",            self._filter_adx_gate(df_5m)),
            ("Regime Gate",         MTFFilterResult(True, "Delegated to engine")),  # engine handles
            ("Volatility Floor",    self._filter_volatility(df_5m)),
            ("Close Breakout",      self._filter_close_breakout(df_5m)),
            ("Daily Limit",         self._filter_daily_limit()),
            ("Cooldown",            self._filter_cooldown()),
            ("Bar Quality",         self._filter_bar_quality(df_5m)),
        ]

        for name, result in filters:
            if not result.passed:
                logger.debug("5M filter FAIL [%s]: %s — %s", self.ticker, name, result.reason)
                return result
            logger.debug("5M filter PASS [%s]: %s — %s", self.ticker, name, result.reason)
        return MTFFilterResult(True, "All 5M filters passed",
                              {"filter_count": len(filters)})

    # ------------------------------------------------------------------
    # Graduation (승급) check
    # ------------------------------------------------------------------
    def check_graduation(self,
                         entry_price_5m: float,
                         atr_5m: float,
                         current_price_5m: float,
                         df_1d: pd.DataFrame,
                         df_1h: pd.DataFrame) -> MTFSignal:
        """Check if 5M position qualifies for graduation to 1D.

        Conditions:
          - 5M profit ≥ +1.5×ATR_5m
          - 1D Donchian breakout active
          - 1H trend maintained
          - Ticker risk ≤ 2.5% of portfolio
        """
        profit_ratio = (current_price_5m / entry_price_5m - 1.0)
        profit_atr = profit_ratio * entry_price_5m / atr_5m if atr_5m > 0 else 0

        if profit_atr < self.GRAD_PROFIT_ATR_MULT:
            return MTFSignal(sleeve="GRAD", signal="NONE", ticker=self.ticker,
                            price=current_price_5m, atr=atr_5m,
                            reason=f"Profit {profit_atr:.1f}×ATR < {self.GRAD_PROFIT_ATR_MULT}×")

        # Check 1D breakout
        try:
            entry_1d = self.check_entry_sleeve(df_1d, self.SLEEVE_1D)
        except Exception:
            return MTFSignal(sleeve="GRAD", signal="NONE", ticker=self.ticker,
                            price=current_price_5m, atr=atr_5m,
                            reason="1D data not available")

        if entry_1d.signal != "LONG":
            return MTFSignal(sleeve="GRAD", signal="NONE", ticker=self.ticker,
                            price=current_price_5m, atr=atr_5m,
                            reason=f"No 1D breakout: {entry_1d.reason}")

        # 1H trend check
        trend_filter = self._filter_trend_alignment(df_1h)
        if not trend_filter.passed:
            return MTFSignal(sleeve="GRAD", signal="NONE", ticker=self.ticker,
                            price=current_price_5m, atr=atr_5m,
                            reason=f"1H trend broken: {trend_filter.reason}")

        return MTFSignal(
            sleeve="GRAD", signal="LONG", ticker=self.ticker,
            price=current_price_5m, atr=atr_5m,
            reason=f"Graduation: 5M→1D (profit {profit_atr:.1f}×ATR, 1D breakout active)",
            details={"profit_atr": profit_atr, "entry_1d_price": entry_1d.price},
        )

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    @staticmethod
    def calc_position_size(equity_krw: float, atr: float, current_price: float,
                          risk_pct: float, max_krw: float = float('inf')) -> int:
        """ATR-based position size with 1/N equal-weight cap.

        max_krw should be equity / max_stocks_per_sleeve (e.g. equity/5 for 1D)
        to prevent low-volatility coins from dominating the portfolio.
        """
        if atr <= 0 or current_price <= 0:
            return 0
        risk_krw = equity_krw * risk_pct
        vol_ratio = atr / current_price
        if vol_ratio <= 0:
            return 0
        unit_krw = risk_krw / vol_ratio
        # 1/N cap: no single position exceeds equity / max_stocks
        capped = min(unit_krw, max_krw)
        return max(int(capped), 5_000)

    @staticmethod
    def calc_sleeve_risk(sleeve_positions: List[dict]) -> float:
        """Calculate total risk of all positions in a sleeve (% of portfolio)."""
        total_risk = 0.0
        for pos in sleeve_positions:
            total_risk += pos.get("risk_pct", 0.0)
        return total_risk

    # ------------------------------------------------------------------
    # Main entry method — used by engine
    # ------------------------------------------------------------------
    def check_1d_entry(self) -> MTFSignal:
        """Check 1D Turtle entry."""
        try:
            df_1d = self._load_ohlcv("ohlcv_1d")
            return self.check_entry_sleeve(df_1d, self.SLEEVE_1D)
        except Exception as e:
            return MTFSignal(sleeve="1D", signal="NONE", ticker=self.ticker,
                            price=0, atr=0, reason=f"Error: {e}")

    def check_5m_entry(self) -> tuple[MTFSignal, MTFFilterResult]:
        """Check 5M Turtle entry with all filters.

        Returns (signal, filter_result) — signal is only LONG if ALL filters pass.
        """
        try:
            df_5m = self._load_ohlcv("ohlcv_5m")
            df_1h = self._load_ohlcv("ohlcv_1h")
        except Exception as e:
            return MTFSignal(sleeve="5M", signal="NONE", ticker=self.ticker,
                            price=0, atr=0, reason=f"Data error: {e}"), \
                   MTFFilterResult(False, f"Data error: {e}")

        # Step 1: Run filters first (cheapest)
        filter_result = self.run_5m_filters(df_5m, df_1h)
        if not filter_result.passed:
            return MTFSignal(sleeve="5M", signal="NONE", ticker=self.ticker,
                            price=0, atr=0, reason=filter_result.reason), \
                   filter_result

        # Step 2: Check breakout
        signal = self.check_entry_sleeve(df_5m, self.SLEEVE_5M)
        signal.details["filter"] = filter_result
        return signal, filter_result

    def check_exit(self, sleeve_cfg: dict, entry_price: float) -> MTFSignal:
        """Check exit for the given sleeve."""
        try:
            table = sleeve_cfg["table"]
            df = self._load_ohlcv(table)
            return self.check_exit_sleeve(df, sleeve_cfg, entry_price)
        except Exception as e:
            return MTFSignal(sleeve="?", signal="HOLD", ticker=self.ticker,
                            price=0, atr=0, reason=f"Exit check error: {e}")

    def check_graduation_trigger(self, entry_price_5m: float, atr_5m: float,
                                 current_price_5m: float) -> MTFSignal:
        """Public wrapper for graduation check."""
        try:
            df_1d = self._load_ohlcv("ohlcv_1d")
            df_1h = self._load_ohlcv("ohlcv_1h")
            return self.check_graduation(entry_price_5m, atr_5m,
                                        current_price_5m, df_1d, df_1h)
        except Exception as e:
            return MTFSignal(sleeve="GRAD", signal="NONE", ticker=self.ticker,
                            price=current_price_5m, atr=atr_5m,
                            reason=f"Graduation check error: {e}")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os, sys
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, _ROOT)

    DB = os.path.join(_ROOT, "data", "upbit_v2.db")

    print("=" * 60)
    print("  🐢🐢 Multi-TF Turtle Strategy — Demo")
    print("=" * 60)

    ticker = "KRW-BTC"
    strat = TurtleMTFStrategy(DB, ticker)

    # -- 1D check --
    print(f"\n📈 1D Sleeve — {ticker}")
    s1d = strat.check_1d_entry()
    print(f"   Signal: {s1d.signal}, Price: {s1d.price:,.0f}, ATR: {s1d.atr:,.0f}")
    print(f"   Reason: {s1d.reason}")

    # -- 5M check --
    print(f"\n⏱️  5M Sleeve — {ticker}")
    s5m, filt = strat.check_5m_entry()
    print(f"   Filter: {'✅' if filt.passed else '❌'} {filt.reason}")
    print(f"   Signal: {s5m.signal}, Price: {s5m.price:,.0f}, ATR: {s5m.atr:,.0f}")
    print(f"   Reason: {s5m.reason}")

    if s5m.signal == "LONG":
        equity = 10_000_000
        pos = strat.calc_position_size(equity, s5m.atr, s5m.price,
                                       strat.SLEEVE_5M["risk_pct"])
        print(f"   Position size (₩{equity:,}): ₩{pos:,}")

    # -- Run on all tickers --
    print(f"\n{'='*60}")
    print("  📊 Full Scan — All 10 tickers")
    print(f"{'='*60}")
    all_tickers = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA",
                   "KRW-DOGE", "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK"]
    results = []
    for t in all_tickers:
        try:
            s = TurtleMTFStrategy(DB, t)
            r1d = s.check_1d_entry()
            r5m, f = s.check_5m_entry()
            results.append((t, r1d, r5m, f))
        except Exception as ex:
            print(f"   ❌ {t}: {ex}")

    for t, r1d, r5m, f in results:
        print(f"\n   {t}")
        print(f"     1D: {r1d.signal:6s} — {r1d.reason}")
        print(f"     5M: {'✅' if f.passed else '❌'} {r5m.signal:6s} — {r5m.reason}")

    print("\n✅ Multi-TF Turtle Demo complete")