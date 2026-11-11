# -*- coding: utf-8 -*-
"""업스트림(GitHub 원본, SMA 기반 ADX) vs 이 포팅판(Wilder ADX) 레짐 판정 차이 정량화.

임시 분석 스크립트 — 판정 후 삭제 가능.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datastore                      # noqa: E402
import indicators as ind              # noqa: E402


def adx_sma(high, low, close, n=14):
    """업스트림 strategies/regime_detector.compute_adx 와 동일한 정의(단순이동평균)."""
    tr = ind.true_range(high, low, close)
    atr = ind.sma(tr, n)
    up = np.append([0.0], np.maximum(high[1:] - high[:-1], 0.0))
    dn = np.append([0.0], np.maximum(low[:-1] - low[1:], 0.0))
    pdm = np.where((up > dn) and True, up, 0.0) if False else np.where((up > dn) * (up > 0), up, 0.0)
    mdm = np.where((dn > up) * (dn > 0), dn, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * ind.sma(pdm, n) / atr
        mdi = 100.0 * ind.sma(mdm, n) / atr
        dx = np.abs(pdi - mdi) / (pdi + mdi) * 100.0
    return ind.sma(dx, n), pdi, mdi


def main() -> int:
    db = "data/upbit.db"
    print(f"{'종목':<10}{'상관':>7}{'평균차':>8}{'최대차':>8}   TRENDING 일수(Wilder vs SMA)  불일치")
    print("-" * 96)
    for ticker in ("KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-AVAX"):
        c = datastore.load(db, "1d", ticker)
        if len(c) < 400:
            continue
        a_w, _, _ = ind.adx(c.high, c.low, c.close, 14)
        a_s, _, _ = adx_sma(c.high, c.low, c.close, 14)
        both = ~np.isnan(a_w) & ~np.isnan(a_s)
        if both.sum() < 100:
            continue
        corr = float(np.corrcoef(a_w[both], a_s[both])[0, 1])
        diff = np.abs(a_w[both] - a_s[both])
        e20, e50 = ind.ema(c.close, 20), ind.ema(c.close, 50)
        aligned = e20 > e50
        t_w = (a_w > 25) & aligned & both
        t_s = (a_s > 25) & aligned & both
        print(f"{ticker:<10}{corr:>7.3f}{diff.mean():>8.1f}{diff.max():>8.1f}"
              f"{int(t_w.sum()):>18} vs {int(t_s.sum()):<8}"
              f"{int((t_w != t_s).sum()):>5}일 / {int(both.sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
