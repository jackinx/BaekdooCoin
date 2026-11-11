# -*- coding: utf-8 -*-
"""백테스트 (원본 backtest_runner.py 대응, 1D 터틀).

  python backtest.py                       # 10종 전체, 자본 1,000,000 KRW
  python backtest.py --tickers KRW-BTC     # 단일
  python backtest.py --from 2023-11-27     # 기간 제한
  python backtest.py --json out.json       # 결과 저장

규칙(원본과 동일):
  진입: 종가 > 직전 55봉 Donchian High (shift 1)
  청산: 종가 < DC20 Low  또는  종가 ≤ 진입가 - 2×ATR
  사이징: risk 1% / (ATR÷가격), 최소 5,000 KRW
  수수료: 편도 0.05% (업비트)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datastore
import indicators as ind
from config import MAJOR_10_TICKERS

FEE = 0.0005          # 편도 0.05%


def run_turtle_1d(cands: datastore.Candles, equity0: float = 1_000_000.0,
                  risk_pct: float = 0.01, dc_in: int = 55, dc_out: int = 20,
                  atr_period: int = 20, atr_mult: float = 2.0,
                  start_date: Optional[str] = None) -> dict:
    n = len(cands)
    warmup = max(dc_in, atr_period, dc_out) + 2
    if n <= warmup:
        return {"ticker": cands.ticker, "trades": 0, "error": "데이터 부족"}

    close, high, low = cands.close, cands.high, cands.low
    atr = ind.atr_wilder(high, low, close, atr_period)
    dc_h, _ = ind.donchian(high, low, dc_in)
    _, dc_l = ind.donchian(high, low, dc_out)

    cash = equity0
    qty = 0.0
    entry = 0.0
    entry_atr = 0.0
    entry_i = -1
    trades: List[dict] = []
    curve = np.full(n, np.nan)

    for i in range(warmup, n):
        if start_date and cands.ts[i][:10] < start_date:
            curve[i] = cash
            continue
        price = float(close[i])
        if qty > 0:
            stop = entry - atr_mult * entry_atr
            if price < float(dc_l[i]) or price <= stop:
                proceeds = qty * price * (1 - FEE)
                cash += proceeds
                pnl = (price * (1 - FEE)) / (entry * (1 + FEE)) - 1
                trades.append({"in": cands.ts[entry_i], "out": cands.ts[i],
                               "entry": entry, "exit": price, "pnl_pct": pnl,
                               "bars": i - entry_i,
                               "reason": "DC20" if price < float(dc_l[i]) else "ATR"})
                qty = 0.0
        else:
            a = float(atr[i])
            if (not math.isnan(a)) and a > 0 and not math.isnan(float(dc_h[i])):
                if float(close[i - 1]) <= float(dc_h[i - 1]) and price > float(dc_h[i]):
                    unit = min(equity0 * risk_pct / (a / price), cash)
                    if unit >= 5_000:
                        qty = unit * (1 - FEE) / price
                        cash -= unit
                        entry, entry_atr, entry_i = price, a, i
        curve[i] = cash + qty * price

    if qty > 0:                                    # 미청산 포지션은 평가가로 마감
        cash += qty * float(close[-1]) * (1 - FEE)
        trades.append({"in": cands.ts[entry_i], "out": cands.ts[-1], "entry": entry,
                       "exit": float(close[-1]),
                       "pnl_pct": (float(close[-1]) * (1 - FEE)) / (entry * (1 + FEE)) - 1,
                       "bars": n - 1 - entry_i, "reason": "OPEN"})
        qty = 0.0

    final = cash
    valid = curve[~np.isnan(curve)]
    peak = np.maximum.accumulate(valid) if len(valid) else np.array([equity0])
    mdd = float(np.max(1 - valid / peak)) * 100 if len(valid) else 0.0
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = -sum(t["pnl_pct"] for t in losses)
    rets = np.diff(valid) / valid[:-1] if len(valid) > 1 else np.array([0.0])
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(365)) if np.std(rets) > 0 else 0.0

    return {
        "ticker": cands.ticker,
        "bars": n,
        "period": f"{cands.ts[warmup][:10]} ~ {cands.ts[-1][:10]}",
        "trades": len(trades),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else (float("inf") if gross_win else 0.0),
        "total_return_pct": round((final / equity0 - 1) * 100, 2),
        "mdd_pct": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "avg_bars": round(float(np.mean([t["bars"] for t in trades])), 1) if trades else 0.0,
        "best_pct": round(max((t["pnl_pct"] for t in trades), default=0) * 100, 2),
        "worst_pct": round(min((t["pnl_pct"] for t in trades), default=0) * 100, 2),
        "final_equity": round(final),
        "_trades": trades,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "upbit.db"))
    ap.add_argument("--tickers", nargs="*", default=MAJOR_10_TICKERS)
    ap.add_argument("--equity", type=float, default=1_000_000)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    results = []
    print(f"{'코인':<10}{'거래':>5}{'승률':>8}{'PF':>7}{'수익률':>10}{'MDD':>8}{'Sharpe':>8}  기간")
    print("-" * 78)
    for t in args.tickers:
        c = datastore.load(args.db, "1d", t)
        if len(c) < 80:
            print(f"{t:<10}  데이터 부족({len(c)}봉) — 수집 필요")
            continue
        r = run_turtle_1d(c, equity0=args.equity, risk_pct=args.risk, start_date=args.start)
        results.append(r)
        print(f"{t.replace('KRW-', ''):<10}{r['trades']:>5}{r['win_rate']:>7.1f}%{r['pf']:>7.2f}"
              f"{r['total_return_pct']:>9.1f}%{r['mdd_pct']:>7.1f}%{r['sharpe']:>8.2f}  {r['period']}")

    if results:
        avg_ret = float(np.mean([r["total_return_pct"] for r in results]))
        tot_trades = sum(r["trades"] for r in results)
        print("-" * 78)
        print(f"평균 수익률 {avg_ret:+.1f}% · 총 거래 {tot_trades}건 · 종목 {len(results)}개")
        if args.verbose:
            for r in results:
                print(f"\n[{r['ticker']}]")
                for t in r["_trades"]:
                    print(f"  {t['in'][:10]} → {t['out'][:10]}  {t['pnl_pct'] * 100:+6.2f}%  "
                          f"{t['bars']:>3}봉  {t['reason']}")
        if args.json:
            out = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
            Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n결과 저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
