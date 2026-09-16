#!/usr/bin/env python3
"""백두코인봇 v2 DB 재백테스트 — Squeeze + BB+RSI (9년 1h 데이터)"""
import sys, os, sqlite3, time, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.WARNING)

from strategies.turtle import TurtleStrategy
from strategies.squeeze_momentum import SqueezeMomentumStrategy
from strategies.bb_rsi_reversal import BBRSIReversalStrategy

DB = "data/upbit_v2.db"
TICKERS = ["KRW-BTC","KRW-ETH","KRW-SOL","KRW-XRP","KRW-ADA","KRW-DOGE","KRW-AVAX","KRW-DOT","KRW-POL","KRW-LINK"]

print("="*70)
print("백두코인봇 v2 재백테스트 — 9년 풀히스토리 (upbit_v2.db)")
print("="*70)

for strategy_cls, name, tf in [
    (TurtleStrategy, "🐢 Turtle (1D)", "1d"),
    (SqueezeMomentumStrategy, "📦 Squeeze (1H)", "1h"),
    (BBRSIReversalStrategy, "🔄 BB+RSI (1H)", "1h"),
]:
    print(f"\n{'='*70}")
    print(f" {name}")
    print(f"{'='*70}")
    print(f"{'Ticker':<12} {'거래':>5} {'승률':>7} {'PF':>7} {'수익률':>9} {'MDD':>7} {'Sharpe':>7}")
    print("-"*70)

    total_trades = 0
    for ticker in TICKERS:
        s = strategy_cls(DB, ticker)
        try:
            result = s.run_backtest()
            trades = result.get("total_trades", 0)
            total_trades += trades
            wr = result.get('win_rate', 0)
            tr = result.get('total_return', 0)
            mdd = result.get('max_drawdown', 0)
            pf = result.get('profit_factor', 0)
            sr = result.get('sharpe', 0)
            print(f"{ticker:<12} {trades:>5} {wr:>6.1f}% "
                  f"{pf:>7.2f} {tr:>8.1f}% "
                  f"{mdd:>6.1f}% {sr:>7.2f}")
        except Exception as e:
            print(f"{ticker:<12} {'ERROR':>5} — {str(e)[:50]}")
    print("-"*70)
    print(f"{'합계':<12} {total_trades:>5}")

print("\n✅ 재백테스트 완료")