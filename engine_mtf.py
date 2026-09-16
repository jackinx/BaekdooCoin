"""
백두코인봇 — Multi-TF Trading Engine (1D + 5M Dual Sleeve)
==========================================================

1D 터틀 + 5M 터틀 듀얼 슬리브 엔진.
기존 engine.py의 백두코인봇Engine을 확장하지 않고 독립 모듈로 작성 —
향후 기존 엔진을 완전히 대체.

슬리브:
  - 1D: 55일 Donchian, 1.0% risk/entry, pyramid 2U, max 5 stocks, 6% sleeve cap
  - 5M: 55-봉 Donchian, 0.30% risk/entry, no pyramid, max 3 stocks, 1.5% sleeve cap
  - Portfolio total: ≤ 7%

Cycle flow (per ticker):
  0. Emergency stop check
  1. Regime check (1H) — only TRENDING → Turtle (new entry only)
  2. Open position? → Full management (exit + pyramid + avg-down + graduation)
     → Always, regardless of regime!
  3. New entry: 1D first → 5M with 7 filters
  4. Portfolio risk cap check on every entry

피라미딩/물타기:
  - 불타기(BUY_UP): +0.5×ATR 상승 시 1유닛 추가 (최대 max_pyramid)
  - 물타기(BUY_DOWN): -0.7~-2.1×ATR 하락 시 1유닛 추가 (최대 max_pyramid+2)

계좌 동기화:
  - sync_from_exchange(): 재시작 시 업비트 실제 보유종목을 리스크매니저에 로드

사용법:
    from engine_mtf import MTFEngine
    engine = MTFEngine(db_path="data/upbit_v2.db", tickers=[...], equity_krw=10_000_000)
    result = engine.run_cycle()
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# Import MTF strategy & risk
from strategies.turtle_mtf import TurtleMTFStrategy, MTFSignal, MTFFilterResult
from strategies.risk_mtf import MTFRiskManager, PortfolioSnapshot
from strategies.regime_detector import RegimeDetector, MarketRegime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order dataclass
# ---------------------------------------------------------------------------

@dataclass
class MTFOrder:
    ticker: str
    side: str              # "BUY" | "SELL" | "GRADUATE_SELL_5M" | "GRADUATE_BUY_1D"
    sleeve: str            # "1D" | "5M" | "GRAD"
    amount_krw: int
    price: float
    reason: str
    atr: float = 0.0
    risk_pct: float = 0.0
    client_order_id: str = ""


@dataclass
class MTFCycleResult:
    timestamp: str
    equity_krw: float
    snapshot: PortfolioSnapshot
    orders: List[MTFOrder]
    regimes: Dict[str, str]
    summary: str
    emergency_stop: Optional[str] = None


# ---------------------------------------------------------------------------
# MTF Engine
# ---------------------------------------------------------------------------

class MTFEngine:
    """멀티TF 트레이딩 엔진: 1D + 5M 듀얼 슬리브."""

    EMERGENCY_STOP_FILE = "/tmp/baekdoocoin_emergency_stop"

    # Pyramid / averaging-down thresholds
    ATR_DOWN = 0.7      # buy-down trigger: -0.7×ATR per level from entry
    ATR_UP = 0.5        # buy-up (pyramid) trigger: +0.5×ATR above entry/last-high
    MAX_EXTRA_DOWN = 2  # max additional averaging-down units (beyond max_pyramid)

    # Fixed-ratio pyramid sizing: unit N → equity * RATIO[N]
    # unit 1 → 1/16, unit 2 → 1/16, unit 3 → 2/16
    PYRAMID_RATIOS = {1: 1/16, 2: 1/16, 3: 2/16}

    def __init__(
        self,
        db_path: str,
        tickers: list[str],
        equity_krw: float,
        *,
        regime_detector: Optional[RegimeDetector] = None,
        max_daily_trades: int = 30,
        max_daily_loss_pct: float = 0.05,
    ):
        if not tickers:
            raise ValueError("tickers list cannot be empty")
        if equity_krw <= 0:
            raise ValueError(f"equity_krw must be positive, got {equity_krw}")

        self.db_path = db_path
        self.tickers = tickers
        self.equity_krw = equity_krw

        self.regime_detector = regime_detector or RegimeDetector()
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_pct = max_daily_loss_pct

        # Risk manager
        self.risk = MTFRiskManager(equity_krw=equity_krw)

        # Strategy cache: {ticker: TurtleMTFStrategy}
        self._strategies: Dict[str, TurtleMTFStrategy] = {}

        # Position tracking (mirrors risk manager)
        self._pos_lock = threading.Lock()

        # Daily tracking
        self._daily_trades_count = 0
        self._daily_last_date = ""
        self._daily_starting_equity = equity_krw
        self._emergency_stop_active = False

        logger.info(
            "MTF Engine init: tickers=%d, equity=₩%d, "
            "1D cap=6%%, 5M cap=1.5%%, portfolio cap=7%%",
            len(tickers), int(equity_krw),
        )

    # ------------------------------------------------------------------
    # Strategy cache
    # ------------------------------------------------------------------
    def _get_strategy(self, ticker: str) -> TurtleMTFStrategy:
        if ticker not in self._strategies:
            self._strategies[ticker] = TurtleMTFStrategy(self.db_path, ticker)
        return self._strategies[ticker]

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    _ENGINE_LOAD_LIMITS = {"ohlcv_1h": 300, "ohlcv_1d": 200, "ohlcv_5m": 500}

    def _load_data(self, ticker: str, table: str) -> pd.DataFrame:
        import sqlite3
        limit_rows = self._ENGINE_LOAD_LIMITS.get(table, 500)
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
        df = pd.read_sql_query(query, conn, params=(ticker,))
        conn.close()
        if df.empty:
            raise ValueError(f"No data: {ticker}/{table}")
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------
    def _check_emergency_stop(self) -> Optional[str]:
        if os.path.exists(self.EMERGENCY_STOP_FILE):
            return f"Kill-switch: {self.EMERGENCY_STOP_FILE}"
        if self._emergency_stop_active:
            return "Emergency stop active"
        if self.equity_krw < self._daily_starting_equity * (1 - self.max_daily_loss_pct):
            loss_pct = (1 - self.equity_krw / self._daily_starting_equity) * 100
            return f"Daily loss limit: -{loss_pct:.1f}%"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_last_date:
            self._daily_trades_count = 0
            self._daily_last_date = today
            self._daily_starting_equity = self.equity_krw
        if self._daily_trades_count >= self.max_daily_trades:
            return f"Daily trade limit ({self.max_daily_trades})"
        return None

    # ------------------------------------------------------------------
    # Exchange position sync
    # ------------------------------------------------------------------
    def sync_from_exchange(self, upbit_client) -> int:
        """Load actual positions from exchange into risk manager.

        Call this after engine init, before starting cycles.
        Returns number of positions synced.
        """
        if not upbit_client:
            return 0
        try:
            balances = upbit_client.get_all_balances()
        except Exception as e:
            logger.warning("Failed to fetch exchange balances for sync: %s", e)
            return 0

        synced = 0
        for ticker, info in balances.items():
            qty = info.get('qty', 0)
            value_krw = info.get('value_krw', 0)
            avg_price = info.get('avg_price', 0)
            if qty <= 0 or value_krw <= 100 or avg_price <= 0:
                continue
            if ticker in self.risk._positions:
                continue

            sleeve = "1D"
            risk_pct = min(value_krw / self.equity_krw, 0.02) if self.equity_krw > 0 else 0.01
            try:
                df_5m = self._load_data(ticker, "ohlcv_5m")
                atr = float(TurtleMTFStrategy.atr(df_5m, 20).iloc[-1])
            except Exception:
                atr = avg_price * 0.02

            self.risk.add_position(
                ticker=ticker, sleeve=sleeve, entry_price=avg_price,
                atr=atr, amount_krw=int(value_krw), risk_pct=risk_pct, units=1,
            )
            synced +=1
            logger.info("Synced from exchange: %s [1D] ₩%d @ ₩%d",
                        ticker, int(value_krw), int(avg_price))
        return synced

    # ------------------------------------------------------------------
    # Position management (exit + pyramid-up + averaging-down + grad)
    # ------------------------------------------------------------------
    def _manage_position(self, ticker: str, pos, strat: TurtleMTFStrategy,
                         summary_lines: list, orders: list, sleeve_cfg: dict) -> None:
        """Full position management: exit, pyramid, averaging-down, graduation."""
        exit_signal = strat.check_exit(sleeve_cfg, pos.entry_price)
        current_price = exit_signal.price
        current_atr = exit_signal.atr

        # ── Exit check ──
        if exit_signal.signal == "EXIT":
            orders.append(MTFOrder(
                ticker=ticker, side="SELL", sleeve=pos.sleeve,
                amount_krw=pos.amount_krw, price=current_price,
                reason=exit_signal.reason, atr=current_atr,
            ))
            self.risk.remove_position(ticker)
            strat.record_trade(
                "STOP_LOSS" if "stop" in exit_signal.reason.lower() else "EXIT")
            self._daily_trades_count += 1
            summary_lines.append(f"📭 {ticker} [{pos.sleeve}] EXIT: {exit_signal.reason}")
            return

        # Track highest price
        if current_price > pos.highest_price:
            self.risk.update_position(ticker, highest_price=current_price)
            pos.highest_price = current_price

        pnl_pct = (current_price / pos.entry_price - 1.0) * 100
        max_pyramid = sleeve_cfg.get("max_pyramid", 1)
        max_units = max_pyramid + self.MAX_EXTRA_DOWN

        # ── Pyramid BUY_UP (불타기): profit, add when price rises by +ATR_UP×ATR ──
        if pnl_pct > 0 and pos.units < max_pyramid + 1:
            reference_price = max(pos.entry_price, pos.highest_price * 0.98)
            up_trigger = reference_price + self.ATR_UP * current_atr
            if current_price >= up_trigger:
                ratio = self.PYRAMID_RATIOS.get(pos.units + 1, 1/16)
                max_krw = self.equity_krw / sleeve_cfg.get("max_stocks", 5)
                raw_size = int(self.equity_krw * ratio)
                # 1/N cap: accumulated position must not exceed equity / max_stocks
                total_after = pos.amount_krw + raw_size
                if total_after > max_krw * 1.5:  # allow 50% exceedance for pyramid
                    add_size = max(int(max_krw * 1.5 - pos.amount_krw), 5_000)
                else:
                    add_size = max(raw_size, 5_000)
                orders.append(MTFOrder(
                    ticker=ticker, side="BUY", sleeve=pos.sleeve,
                    amount_krw=add_size, price=current_price,
                    reason=f"🔥 불타기: +{pnl_pct:+.1f}% (price>{up_trigger:,.0f}, eq×{ratio:.4f})",
                    atr=current_atr,
                ))
                self.risk.update_position(
                    ticker, amount_krw=pos.amount_krw + add_size,
                    units=pos.units + 1, highest_price=current_price,
                )
                strat.record_trade("BUY_UP")
                self._daily_trades_count += 1
                summary_lines.append(
                    f"🔥 {ticker} [{pos.sleeve}] BUY_UP: +{pnl_pct:+.1f}% "
                    f"unit{pos.units+1}/{max_units} (₩{add_size:,})")
                return

        # ── Averaging-down BUY_DOWN (물타기): loss, add when price drops ──
        if pnl_pct < 0 and pos.units < max_units:
            threshold = pos.entry_price - (pos.units * self.ATR_DOWN + self.ATR_DOWN) * current_atr
            if current_price <= threshold:
                ratio = self.PYRAMID_RATIOS.get(pos.units + 1, 1/16)
                max_krw = self.equity_krw / sleeve_cfg.get("max_stocks", 5)
                raw_size = int(self.equity_krw * ratio)
                # 1/N cap with 1.5x allowance for averaging-down
                total_after = pos.amount_krw + raw_size
                if total_after > max_krw * 1.5:
                    add_size = max(int(max_krw * 1.5 - pos.amount_krw), 5_000)
                else:
                    add_size = max(raw_size, 5_000)
                orders.append(MTFOrder(
                    ticker=ticker, side="BUY", sleeve=pos.sleeve,
                    amount_krw=add_size, price=current_price,
                    reason=f"💧 물타기: {pnl_pct:+.1f}% "
                           f"(price<{threshold:,.0f}, unit{pos.units+1}/{max_units})",
                    atr=current_atr,
                ))
                self.risk.update_position(
                    ticker, amount_krw=pos.amount_krw + add_size, units=pos.units + 1,
                )
                strat.record_trade("BUY_DOWN")
                self._daily_trades_count += 1
                summary_lines.append(
                    f"💧 {ticker} [{pos.sleeve}] BUY_DOWN: {pnl_pct:+.1f}% "
                    f"unit{pos.units+1}/{max_units} (₩{add_size:,})")
                return

        # ── 5M Graduation check ──
        if pos.sleeve == "5M":
            grad = strat.check_graduation_trigger(pos.entry_price, pos.atr, current_price)
            if grad.signal == "LONG":
                new_1d_risk = TurtleMTFStrategy.SLEEVE_1D["risk_pct"]
                ok, reason = self.risk.approve_graduation(ticker, new_1d_risk)
                if ok:
                    orders.append(MTFOrder(
                        ticker=ticker, side="GRADUATE_SELL_5M", sleeve="GRAD",
                        amount_krw=pos.amount_krw, price=current_price,
                        reason=grad.reason, atr=grad.atr,
                    ))
                    orders.append(MTFOrder(
                        ticker=ticker, side="GRADUATE_BUY_1D", sleeve="GRAD",
                        amount_krw=pos.amount_krw,
                        price=grad.details.get("entry_1d_price", current_price),
                        reason="Graduation: 5M→1D", atr=grad.atr,
                        risk_pct=new_1d_risk,
                    ))
                    self._daily_trades_count += 2
                    summary_lines.append(f"🎓 {ticker} GRADUATION 5M→1D: {grad.reason}")
                    return
                else:
                    summary_lines.append(f"⏳ {ticker} [5M] graduation blocked: {reason}")
                    return

        summary_lines.append(
            f"💼 {ticker} [{pos.sleeve}] holding (₩{pos.amount_krw:,}, {pnl_pct:+.1f}%)")

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------
    def run_cycle(self) -> MTFCycleResult:
        """Execute one full trading cycle across all tickers."""
        import time as _time
        _t0 = _time.monotonic()

        # Emergency stop
        stop_reason = self._check_emergency_stop()
        if stop_reason:
            snap = self.risk.snapshot()
            return MTFCycleResult(
                timestamp=datetime.now(timezone.utc).isoformat(),
                equity_krw=self.equity_krw,
                snapshot=snap,
                orders=[],
                regimes={},
                summary=f"🚨 EMERGENCY STOP: {stop_reason}",
                emergency_stop=stop_reason,
            )

        timestamp = datetime.now(timezone.utc).isoformat()
        orders: List[MTFOrder] = []
        regimes: Dict[str, str] = {}
        summary_lines: List[str] = []

        # Update risk manager equity
        self.risk.equity_krw = self.equity_krw

        for ticker in self.tickers:
            try:
                # ── Regime detection (1H) ──
                try:
                    df_1h = self._load_data(ticker, "ohlcv_1h")
                    regime, indicators = self.regime_detector.detect_regime(df_1h)
                    regimes[ticker] = regime.value
                except Exception:
                    regimes[ticker] = "ERROR"
                    continue

                strat = self._get_strategy(ticker)

                # ── Existing position management (ALWAYS, regardless of regime) ──
                pos = self.risk.get_position(ticker)
                if pos is not None:
                    sleeve_cfg = (
                        TurtleMTFStrategy.SLEEVE_1D if pos.sleeve == "1D"
                        else TurtleMTFStrategy.SLEEVE_5M
                    )
                    self._manage_position(ticker, pos, strat, summary_lines, orders, sleeve_cfg)
                    continue

                # ── New entry: regime gate ──
                # TRENDING → normal Turtle entry (1D + 5M)
                # Other regimes → fallback: 1D DC55 breakout override
                if regime != MarketRegime.TRENDING:
                    try:
                        # 1D DC55 Donchian high breakout check (regime-agnostic)
                        signal_1d_override = strat.check_1d_entry()
                        if signal_1d_override.signal == "LONG":
                            risk_pct_1d = TurtleMTFStrategy.SLEEVE_1D["risk_pct"]
                            max_krw_1d = self.equity_krw / TurtleMTFStrategy.SLEEVE_1D["max_stocks"]
                            pos_size = TurtleMTFStrategy.calc_position_size(
                                self.equity_krw, signal_1d_override.atr,
                                signal_1d_override.price, risk_pct_1d, max_krw_1d,
                            )
                            ok, adj_size, reason = self.risk.approve_entry(
                                "1D", ticker, pos_size, risk_pct_1d,
                            )
                            if ok:
                                orders.append(MTFOrder(
                                    ticker=ticker, side="BUY", sleeve="1D",
                                    amount_krw=adj_size,
                                    price=signal_1d_override.price,
                                    reason=signal_1d_override.reason,
                                    atr=signal_1d_override.atr,
                                    risk_pct=risk_pct_1d,
                                ))
                                self.risk.add_position(
                                    ticker, "1D", signal_1d_override.price,
                                    signal_1d_override.atr, adj_size,
                                    risk_pct_1d,
                                )
                                strat.record_trade("1D_ENTRY_OVERRIDE")
                                self._daily_trades_count += 1
                                summary_lines.append(
                                    f"🐢 {ticker} 1D LONG (DC Override): "
                                    f"{signal_1d_override.reason} "
                                    f"(₩{adj_size:,})")
                                continue
                            else:
                                summary_lines.append(
                                    f"🚫 {ticker} {regime.value} 1D override "
                                    f"blocked: {reason}")
                        else:
                            summary_lines.append(
                                f"⏸️ {ticker}: {regime.value} (skip — "
                                f"1D DC: {signal_1d_override.reason})")
                    except Exception:
                        summary_lines.append(
                            f"⏸️ {ticker}: {regime.value} (skip MTF)")
                    continue

                # ── 1D entry first (higher timeframe priority) ──
                signal_1d = strat.check_1d_entry()
                if signal_1d.signal == "LONG":
                    risk_pct_1d = TurtleMTFStrategy.SLEEVE_1D["risk_pct"]
                    max_krw_1d = self.equity_krw / TurtleMTFStrategy.SLEEVE_1D["max_stocks"]
                    pos_size = TurtleMTFStrategy.calc_position_size(
                        self.equity_krw, signal_1d.atr, signal_1d.price, risk_pct_1d, max_krw_1d,
                    )
                    ok, adj_size, reason = self.risk.approve_entry(
                        "1D", ticker, pos_size, risk_pct_1d,
                    )
                    if ok:
                        orders.append(MTFOrder(
                            ticker=ticker, side="BUY", sleeve="1D",
                            amount_krw=adj_size, price=signal_1d.price,
                            reason=signal_1d.reason, atr=signal_1d.atr,
                            risk_pct=risk_pct_1d,
                        ))
                        self.risk.add_position(
                            ticker, "1D", signal_1d.price, signal_1d.atr,
                            adj_size, risk_pct_1d,
                        )
                        strat.record_trade("1D_ENTRY")
                        self._daily_trades_count += 1
                        summary_lines.append(
                            f"🐢 {ticker} 1D LONG: {signal_1d.reason} (₩{adj_size:,})")
                    else:
                        summary_lines.append(f"🚫 {ticker} 1D blocked: {reason}")
                    continue

                # ── 5M entry with filters ──
                signal_5m, filter_result = strat.check_5m_entry()
                if signal_5m.signal == "LONG":
                    risk_pct_5m = TurtleMTFStrategy.SLEEVE_5M["risk_pct"]
                    max_krw_5m = self.equity_krw / TurtleMTFStrategy.SLEEVE_5M["max_stocks"]
                    pos_size = TurtleMTFStrategy.calc_position_size(
                        self.equity_krw, signal_5m.atr, signal_5m.price, risk_pct_5m, max_krw_5m,
                    )
                    ok, adj_size, reason = self.risk.approve_entry(
                        "5M", ticker, pos_size, risk_pct_5m,
                    )
                    if ok:
                        orders.append(MTFOrder(
                            ticker=ticker, side="BUY", sleeve="5M",
                            amount_krw=adj_size, price=signal_5m.price,
                            reason=f"{signal_5m.reason} | {filter_result.reason}",
                            atr=signal_5m.atr, risk_pct=risk_pct_5m,
                        ))
                        self.risk.add_position(
                            ticker, "5M", signal_5m.price, signal_5m.atr,
                            adj_size, risk_pct_5m,
                        )
                        strat.record_trade("5M_ENTRY")
                        self._daily_trades_count += 1
                        summary_lines.append(
                            f"🐇 {ticker} 5M LONG: {signal_5m.reason} (₩{adj_size:,})")
                    else:
                        summary_lines.append(f"🚫 {ticker} 5M blocked: {reason}")
                else:
                    summary_lines.append(f"⏸️ {ticker}: 5M {signal_5m.reason}")

            except Exception as e:
                logger.error("%s error: %s", ticker, e)
                regimes[ticker] = "ERROR"
                summary_lines.append(f"❌ {ticker}: {e}")

        # ── Final snapshot ──
        snap = self.risk.snapshot()
        summary = "\n".join(summary_lines) if summary_lines else "No signals"

        elapsed = _time.monotonic() - _t0
        logger.info("Cycle 완료: %d 주문 (%.1fs)", len(orders), elapsed)

        return MTFCycleResult(
            timestamp=timestamp,
            equity_krw=self.equity_krw,
            snapshot=snap,
            orders=orders,
            regimes=regimes,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def get_status_text(self) -> str:
        """Return a multi-line status string for Telegram /status."""
        snap = self.risk.snapshot()
        lines = [
            "🦅 백두코인봇 MTF 엔진 상태",
            f"💰 자본금: ₩{self.equity_krw:,.0f}",
            f"📊 포트폴리오 리스크: {snap.total_risk_pct*100:.1f}%/7.0%",
            f"  1D 슬리브: {snap.sleeve_1d_risk_pct*100:.1f}%/6.0% ({snap.positions_1d}/5 종목)",
            f"  5M 슬리브: {snap.sleeve_5m_risk_pct*100:.1f}%/1.5% ({snap.positions_5m}/3 종목)",
            f"💰 커밋금액: ₩{snap.total_committed_krw:,}",
            f"📅 오늘 거래: {self._daily_trades_count}회",
        ]
        if snap.positions_1d + snap.positions_5m > 0:
            lines.append("\n📂 열린 포지션:")
            for p in self.risk.get_all_positions():
                lines.append(
                    f"  [{p.sleeve}] {p.ticker} ₩{p.amount_krw:,} "
                    f"risk {p.risk_pct*100:.1f}%")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os, sys
    _ROOT = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _ROOT)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    DB = os.path.join(_ROOT, "data", "upbit_v2.db")
    TICKERS = [
        "KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA",
        "KRW-DOGE", "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK",
    ]

    print("=" * 60)
    print("  🦅 Multi-TF Engine — Demo")
    print("=" * 60)

    engine = MTFEngine(
        db_path=DB,
        tickers=TICKERS,
        equity_krw=10_000_000,
    )

    print(f"\n🔄 Trading cycle 실행 중...\n")
    result = engine.run_cycle()

    print("=" * 60)
    print("  📋 레짐 감지 결과")
    print("=" * 60)
    for ticker, reg in result.regimes.items():
        emoji = {"TRENDING": "📈", "SQUEEZING": "📦", "REVERSING": "🔄",
                 "NEUTRAL": "⏸️", "ERROR": "❌"}
        print(f"  {emoji.get(reg, '❓')} {ticker}: {reg}")

    print(f"\n{'='*60}")
    print(f"  🛒 생성된 주문 ({len(result.orders)}건)")
    print(f"{'='*60}")
    for o in result.orders:
        print(f"  [{o.sleeve}] {o.ticker} {o.side} ₩{o.amount_krw:,} @ ₩{o.price:,.0f}")
        print(f"  → {o.reason}")

    print(f"\n{'='*60}")
    print(f"  📊 리스크 현황")
    print(f"{'='*60}")
    snap = result.snapshot
    print(f"  Total:     {snap.total_risk_pct*100:.1f}%/7.0%")
    print(f"  1D Sleeve: {snap.sleeve_1d_risk_pct*100:.1f}%/6.0% ({snap.positions_1d} pos)")
    print(f"  5M Sleeve: {snap.sleeve_5m_risk_pct*100:.1f}%/1.5% ({snap.positions_5m} pos)")

    print(f"\n{'='*60}")
    print(f"  📝 요약")
    print(f"{'='*60}")
    print(result.summary)

    print("\n✅ Multi-TF Engine demo complete")