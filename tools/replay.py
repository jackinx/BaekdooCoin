# -*- coding: utf-8 -*-
"""엔진 리플레이(페이퍼 트레이딩) — 실측 일봉 위에서 실제 엔진 코드를 돌린다.

  python tools/replay.py --from 2023-01-01 --to 2026-10-30 --equity 1000000

- DB의 1D 캔들로 하루씩 as_of 를 밀어가며 MTFEngine.run_cycle() 을 호출
- 주문은 그날 종가에 즉시 체결된 것으로 처리 (수수료 편도 0.05%)
- 런타임 파일(포지션/킬스위치)은 임시 디렉터리로 우회 → 실운영 상태를 건드리지 않음
- 1H/5M 데이터가 없으면 자동으로 1D 슬리브만 동작한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import datastore                       # noqa: E402
import paths                           # noqa: E402
from config import MAJOR_10_TICKERS    # noqa: E402
from engine_mtf import MTFEngine       # noqa: E402

FEE = 0.0005


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "upbit.db"))
    ap.add_argument("--tickers", nargs="*", default=MAJOR_10_TICKERS)
    ap.add_argument("--from", dest="start", default="2023-01-01")
    ap.add_argument("--to", dest="end", default="2026-12-31")
    ap.add_argument("--equity", type=float, default=1_000_000)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    # 런타임 파일 격리 (실운영 포지션/킬스위치 보호)
    sandbox = paths.LOG_DIR / "replay_runtime"
    sandbox.mkdir(parents=True, exist_ok=True)
    paths.RUNTIME_DIR = sandbox
    paths.POSITIONS_FILE = sandbox / "open_positions.json"
    paths.STATE_FILE = sandbox / "engine_state.json"
    paths.EMERGENCY_STOP_FILE = sandbox / "EMERGENCY_STOP"
    paths.PAUSE_FILE = sandbox / "PAUSED"
    paths.TRADES_FILE = sandbox / "trades_replay.jsonl"   # 실거래 원장 오염 방지
    for f in (paths.POSITIONS_FILE, paths.STATE_FILE, paths.EMERGENCY_STOP_FILE,
              paths.TRADES_FILE, sandbox / "protections.json"):
        f.unlink(missing_ok=True)          # 이전 실행의 보호장치 정지 상태까지 초기화

    eng = MTFEngine(tickers=args.tickers, db_path=args.db, equity_krw=args.equity)

    # 날짜 축 (BTC 1d 기준)
    base = datastore.load(args.db, "1d", args.tickers[0])
    dates = [ts[:10] for ts in base.ts if args.start <= ts[:10] <= args.end]
    if not dates:
        print("해당 기간 데이터 없음")
        return 1

    closes: Dict[str, Dict[str, float]] = {}
    for t in args.tickers:
        c = datastore.load(args.db, "1d", t)
        closes[t] = {ts[:10]: float(px) for ts, px in zip(c.ts, c.close)}

    cash = args.equity
    trades: List[dict] = []
    curve: List[dict] = []

    for d in dates:
        eng.as_of = d
        # 리플레이에서는 하루를 '거래일 1일'로 취급 → 일일 카운터/손실기준 리셋
        from datetime import date as _date, datetime
        eng._daily_date = _date.fromisoformat(d)
        sim_now = datetime.fromisoformat(d + "T12:00:00+00:00")
        eng.now_fn = lambda _t=sim_now: _t          # 보호장치도 시뮬레이션 시각 사용
        eng._daily_starting_equity = eng.equity_krw
        eng._daily_trades_count = 0
        prices = {t: closes[t][d] for t in args.tickers if d in closes[t]}
        if not prices:
            continue
        res = eng.run_cycle(live_prices=prices, cash_krw=cash)
        if res.get("emergency_stop"):
            print(f"⛔ {d} 비상정지: {res['emergency_stop']}")
            break

        for o in res["orders"]:
            t, kind, px = o["ticker"], o["kind"], float(o.get("price") or prices.get(t, 0))
            if px <= 0:
                continue
            if kind in ("BUY", "BUY_UP", "BUY_DOWN", "GRADUATE"):
                amt = float(o["amount_krw"])
                if kind == "GRADUATE":
                    old = eng.risk.get(t)
                    if old:
                        cash += old.extras.get("qty", 0.0) * px * (1 - FEE)
                        trades.append({"date": d, "ticker": t, "kind": "GRADUATE_SELL_5M",
                                       "price": px, "qty": old.extras.get("qty", 0.0)})
                if amt > cash:
                    # 실전과 동일하게 '주문 실패'로 처리 (조용한 절단 금지 — 감사 F19)
                    trades.append({"date": d, "ticker": t, "kind": kind, "price": px,
                                   "amount_krw": amt, "reason": "REJECTED: 현금 부족"})
                    continue
                if amt < 5_000:
                    continue
                o["amount_krw"] = amt
                cash -= amt
                trades.append({"date": d, "ticker": t, "kind": kind, "price": px,
                               "amount_krw": amt, "reason": o.get("reason", "")})
                eng.on_order_filled(o, fill_price=px)
            else:  # SELL_ALL / SELL_PARTIAL
                pos = eng.risk.get(t)
                if not pos:
                    continue
                ratio = float(o.get("ratio", 1.0))
                qty = pos.extras.get("qty", 0.0) * ratio
                proceeds = qty * px * (1 - FEE)
                cash += proceeds
                trades.append({"date": d, "ticker": t, "kind": kind, "price": px, "qty": qty,
                               "avg_price": pos.avg_price,
                               "pnl_pct": round((px / pos.avg_price - 1) * 100, 2),
                               "reason": o.get("reason", "")})
                eng.on_order_filled(o, fill_price=px)

        hold = sum(p.extras.get("qty", 0.0) * prices.get(t, p.entry_price)
                   for t, p in eng.risk.positions.items())
        eng.equity_krw = cash + hold
        eng.risk.equity_krw = eng.equity_krw
        curve.append({"date": d, "equity": eng.equity_krw, "cash": cash,
                      "positions": len(eng.risk.positions)})

    finals = curve[-1]["equity"] if curve else args.equity
    peak, mdd = 0.0, 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        if peak > 0:
            mdd = max(mdd, 1 - p["equity"] / peak)

    buys = [t for t in trades if t["kind"] in ("BUY", "BUY_UP", "BUY_DOWN")]
    sells = [t for t in trades if t["kind"] in ("SELL_ALL", "SELL_PARTIAL", "GRADUATE_SELL_5M")]
    print(f"\n=== 리플레이 {dates[0]} ~ {dates[-1]} ({len(dates)}일) ===")
    print(f"자본 {args.equity:,.0f} → {finals:,.0f} KRW  ({(finals / args.equity - 1) * 100:+.2f}%)")
    print(f"주문 {len(trades)}건 (매수 {len(buys)} / 매도 {len(sells)}) · MDD {mdd * 100:.1f}% · "
          f"최종 보유 {curve[-1]['positions'] if curve else 0}종")
    kinds: Dict[str, int] = {}
    for t in trades:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    print("주문 종류:", kinds)
    if sells:
        wins = [s for s in sells if s.get("pnl_pct", 0) > 0]
        print(f"매도 {len(sells)}건 중 익절 {len(wins)}건 "
              f"(승률 {100 * len(wins) / len(sells):.1f}%)")
    print("\n최근 주문 15건:")
    for t in trades[-15:]:
        extra = f"{t.get('amount_krw', 0):,.0f} KRW" if "amount_krw" in t else f"{t.get('qty', 0):.6f}개"
        pnl = f" pnl {t['pnl_pct']:+.2f}%" if "pnl_pct" in t else ""
        print(f"  {t['date']} {t['ticker']:<10} {t['kind']:<16} {extra}{pnl}  {t.get('reason', '')[:60]}")

    if args.json:
        Path(args.json).write_text(json.dumps({"trades": trades, "curve": curve},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
