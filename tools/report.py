# -*- coding: utf-8 -*-
"""거래 원장 리포트 — 성과/수수료/세금 (감사 F18, 벤치마크 갭).

  python tools/report.py                # 전체 요약 + 종목별 PF/기대값
  python tools/report.py --monthly      # 월별 실현손익
  python tools/report.py --tax          # 연도별 과세 추정 (2027: 22%, 250만원 공제)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
import sys                                        # noqa: E402
sys.path.insert(0, str(ROOT))
import paths                                     # noqa: E402


def load_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: List[dict]) -> None:
    if not rows:
        print(f"거래 원장이 비어 있습니다: {paths.TRADES_FILE}")
        return
    buys = [r for r in rows if r["side"] == "buy"]
    sells = [r for r in rows if r["side"] == "sell"]
    fees = sum(r.get("fee", 0.0) for r in rows)
    realized = sum(r.get("realized_pnl", 0.0) for r in sells)
    print("=" * 72)
    print(f"체결 {len(rows)}건 (매수 {len(buys)} / 매도 {len(sells)}) · "
          f"수수료 {fees:,.0f}원 · 실현손익 {realized:,.0f}원")
    print(f"기간: {rows[0]['ts'][:10]} ~ {rows[-1]['ts'][:10]} · 모드: "
          f"{', '.join(sorted({r.get('mode', '?') for r in rows}))}")
    print("=" * 72)
    print(f"{'종목':<12}{'매도':>5}{'승률':>8}{'PF':>7}{'기대값(원)':>12}{'총손익(원)':>13}")
    print("-" * 72)
    by_ticker: Dict[str, List[dict]] = defaultdict(list)
    for r in sells:
        by_ticker[r["ticker"]].append(r)
    total_pf_w, total_pf_l = 0.0, 0.0
    for ticker, rs in sorted(by_ticker.items(), key=lambda kv: -sum(x["realized_pnl"] for x in kv[1])):
        wins = [x for x in rs if x["realized_pnl"] > 0]
        w = sum(x["realized_pnl"] for x in wins)
        l = -sum(x["realized_pnl"] for x in rs if x["realized_pnl"] <= 0)
        total_pf_w += w
        total_pf_l += l
        pf = (w / l) if l > 0 else (float("inf") if w else 0.0)
        exp = sum(x["realized_pnl"] for x in rs) / len(rs)
        print(f"{ticker.replace('KRW-', ''):<12}{len(rs):>5}{100 * len(wins) / len(rs):>7.1f}%"
              f"{pf:>7.2f}{exp:>12,.0f}{sum(x['realized_pnl'] for x in rs):>13,.0f}")
    print("-" * 72)
    pf_all = (total_pf_w / total_pf_l) if total_pf_l > 0 else float("inf")
    print(f"{'전체':<12}{len(sells):>5}{'':>8}{pf_all:>7.2f}")


def monthly(rows: List[dict]) -> None:
    agg: Dict[str, float] = defaultdict(float)
    for r in rows:
        if r["side"] == "sell":
            agg[r["ts"][:7]] += r.get("realized_pnl", 0.0)
    if not agg:
        print("매도 기록 없음")
        return
    print(f"{'월':<10}{'실현손익(원)':>15}")
    for m in sorted(agg):
        print(f"{m:<10}{agg[m]:>15,.0f}")
    print(f"{'합계':<10}{sum(agg.values()):>15,.0f}")


def tax(rows: List[dict], rate: float = 0.22, deduction: float = 2_500_000.0) -> None:
    """2027-01-01 이후 양도분: 기타소득 분리과세 22%, 연 250만원 공제 (추정)."""
    agg: Dict[str, float] = defaultdict(float)
    for r in rows:
        if r["side"] == "sell":
            agg[r["ts"][:4]] += r.get("realized_pnl", 0.0)
    print(f"{'연도':<8}{'실현손익':>15}{'과세표준':>15}{'추정세액':>12}")
    for y in sorted(agg):
        pnl = agg[y]
        base = max(pnl - deduction, 0.0) if y >= "2027" else 0.0
        print(f"{y:<8}{pnl:>15,.0f}{base:>15,.0f}{base * rate:>12,.0f}")
    print("\n※ 2027-01-01 이후 양도분부터 적용(추정). 실제 신고 전 세무사 확인 필요.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(paths.TRADES_FILE))
    ap.add_argument("--monthly", action="store_true")
    ap.add_argument("--tax", action="store_true")
    args = ap.parse_args()
    rows = load_rows(Path(args.file))
    if args.tax:
        tax(rows)
    elif args.monthly:
        monthly(rows)
    else:
        summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
