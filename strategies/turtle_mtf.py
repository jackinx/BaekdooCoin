# -*- coding: utf-8 -*-
"""멀티TF 터틀 전략 (원본 strategies/turtle_mtf.py).

핵심 파라미터 (원본과 동일)
  1D 슬리브: DC55 진입 / DC20 청산 / risk 1.0% / ATR2.0 손절 / 피라미딩 2 / 최대 5종 / 캡 6%
  5M 슬리브: DC55 진입 / DC20 청산 / risk 0.30% / ATR2.0 / 피라미딩 1 / 최대 3종 / 캡 1.5%
  포트폴리오 총캡 7%

엔트리: 종가가 '직전(당일 제외)' 55기간 Donchian High 상향 돌파
엑시트: 종가 < DC20 Low  또는  종가 ≤ 진입가 - 2×ATR
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import indicators as ind                      # noqa: E402
from datastore import Candles                 # noqa: E402

# ---------------- 슬리브 파라미터 ----------------
SLEEVE_1D: Dict[str, float] = {
    "name": "1D", "donchian_period": 55, "exit_period": 20, "risk_pct": 0.01,
    "atr_mult": 2.0, "max_pyramid": 2, "max_stocks": 5, "sleeve_risk_cap": 0.06,
    "atr_period": 20,
}
SLEEVE_5M: Dict[str, float] = {
    "name": "5M", "donchian_period": 55, "exit_period": 20, "risk_pct": 0.003,
    "atr_mult": 2.0, "max_pyramid": 1, "max_stocks": 3, "sleeve_risk_cap": 0.015,
    "atr_period": 20,
}
PORTFOLIO_TOTAL_CAP = 0.07

# 5M 진입 7종 필터 기본값
FILTER_ADX_MIN = 20.0
FILTER_VOLATILITY_MIN_PCT = 0.05      # ATR/price ≥ 0.05%
FILTER_BAR_RANGE_ATR_MULT = 1.0
FILTER_VOLUME_AVG_MULT = 1.0
FILTER_DAILY_MAX_TRADES = 3
FILTER_COOLDOWN_MINUTES = 60

# 피라미딩 / 물타기
ATR_UP = 0.5
ATR_DOWN = 0.7
MAX_EXTRA_DOWN = 2

MIN_ORDER_KRW = 5_000.0


def calc_position_size(equity_krw: float, atr: float, price: float, risk_pct: float) -> int:
    """ATR 기반 유닛 사이즈. max(risk/vol, 5000), 상한 equity."""
    if not (equity_krw > 0) or not (price > 0) or not (atr > 0) or np.isnan(atr):
        return 0
    risk_krw = equity_krw * risk_pct
    vol_ratio = atr / price
    if vol_ratio <= 0:
        return 0
    unit = risk_krw / vol_ratio
    unit = min(unit, equity_krw)
    return int(max(unit, MIN_ORDER_KRW))


@dataclass
class EntrySignal:
    signal: str            # "LONG" | "NONE"
    price: float
    atr: float
    dc_high: float
    dc_low: float
    reason: str
    sleeve: str = ""
    filters_passed: Optional[List[str]] = None


@dataclass
class ExitSignal:
    signal: str            # "EXIT" | "HOLD"
    reason: str
    price: float = 0.0


class TurtleMTF:
    def __init__(self, sleeve: Dict[str, float]):
        self.s = sleeve
        self.name = str(sleeve["name"])

    # ---------- 지표 ----------
    def _atr(self, c: Candles) -> np.ndarray:
        return ind.atr_wilder(c.high, c.low, c.close, int(self.s["atr_period"]))

    def donchian_last(self, c: Candles) -> Tuple[float, float]:
        """직전(마지막 봉 제외) Donchian High/Low + ATR 마지막 값."""
        p = int(self.s["donchian_period"])
        dh, dl = ind.donchian(c.high, c.low, p)
        hi = float(dh[-1]) if not np.isnan(dh[-1]) else float("nan")
        lo = float(dl[-1]) if not np.isnan(dl[-1]) else float("nan")
        return hi, lo

    # ---------- 진입 ----------
    def check_entry(self, c: Candles, live_price: Optional[float] = None) -> EntrySignal:
        p = int(self.s["donchian_period"])
        if len(c) < p + 2:
            return EntrySignal("NONE", 0, 0, 0, 0, f"데이터 부족({len(c)}<{p + 2})", self.name)

        atr_arr = self._atr(c)
        atr = float(atr_arr[-1])
        dh, _ = ind.donchian(c.high, c.low, p)

        close_prev = float(c.close[-2])
        dc_prev = float(dh[-2])
        close_cur = float(live_price) if live_price else float(c.close[-1])
        dc_cur = float(dh[-1])

        if any(np.isnan(x) for x in (atr, dc_prev, dc_cur)):
            return EntrySignal("NONE", close_cur, atr, dc_cur, float("nan"), "지표 NaN", self.name)

        if close_prev <= dc_prev and close_cur > dc_cur:
            return EntrySignal("LONG", close_cur, atr, dc_cur, float("nan"),
                               f"DC{p} 돌파 {dc_cur:,.4f} (종가 {close_cur:,.4f})", self.name)

        return EntrySignal("NONE", close_cur, atr, dc_cur, float("nan"),
                           f"DC{p} 미돌파 (종가 {close_cur:,.4f} ≤ {dc_cur:,.4f})", self.name)

    def check_entry_5m(self, c5: Candles, c1h: Candles, limiter: "TradeLimiter",
                       now: Optional[datetime] = None) -> Tuple[EntrySignal, List[str], List[str]]:
        """5M 진입 + 7종 필터. 반환 (signal, 통과필터, 실패필터)"""
        now = now or datetime.now(timezone.utc)
        base = self.check_entry(c5)
        passed: List[str] = []
        failed: List[str] = []
        if base.signal != "LONG":
            return base, passed, failed

        price = base.price
        if price <= 0:
            return EntrySignal("NONE", price, base.atr, base.dc_high, 0, "가격 0", self.name), [], ["price"]

        # 1) 1H 추세 정렬
        ema20 = ind.ema(c1h.close, 20)
        ema50 = ind.ema(c1h.close, 50)
        if (not np.isnan(ema20[-1]) and not np.isnan(ema50[-1])
                and c1h.close[-1] > ema50[-1] and ema20[-1] > ema50[-1]):
            passed.append("1H추세")
        else:
            failed.append("1H추세")

        # 2) 5M ADX 게이트
        adx_v, pdi, mdi = ind.adx(c5.high, c5.low, c5.close, 14)
        if not np.isnan(adx_v[-1]) and adx_v[-1] >= FILTER_ADX_MIN and pdi[-1] > mdi[-1]:
            passed.append("ADX")
        else:
            failed.append("ADX")

        # 4) 변동성 하한
        if base.atr > 0 and (base.atr / price) * 100.0 >= FILTER_VOLATILITY_MIN_PCT:
            passed.append("변동성")
        else:
            failed.append("변동성")

        # 5) 종가 기준 돌파 (check_entry 가 이미 종가 기준)
        passed.append("종가돌파")

        # 6) 일일 제한 + 쿨다운
        if limiter.can_enter(c5.ticker, now):
            passed.append("일일제한")
        else:
            failed.append("일일제한")

        # 7) 돌파 품질: 봉 범위 ≥ 1×ATR  OR  거래량 ≥ 20봉 평균
        bar_range = float(c5.high[-1] - c5.low[-1])
        vol_avg = float(np.mean(c5.volume[-20:])) if len(c5) >= 20 else float("nan")
        if bar_range >= FILTER_BAR_RANGE_ATR_MULT * base.atr or (
                not np.isnan(vol_avg) and vol_avg > 0 and c5.volume[-1] >= FILTER_VOLUME_AVG_MULT * vol_avg):
            passed.append("돌파품질")
        else:
            failed.append("돌파품질")

        sig = base if not failed else EntrySignal("NONE", price, base.atr, base.dc_high, 0,
                                                 "필터 실패: " + ",".join(failed), self.name)
        sig.filters_passed = passed
        return sig, passed, failed

    # ---------- 청산 ----------
    def check_exit(self, c: Candles, entry_price: float, atr: Optional[float] = None,
                   live_price: Optional[float] = None) -> ExitSignal:
        p = int(self.s["exit_period"])
        if len(c) < p + 2:
            return ExitSignal("HOLD", "데이터 부족")
        price = float(live_price) if live_price else float(c.close[-1])
        _, dl = ind.donchian(c.high, c.low, p)
        dc_low = float(dl[-1])
        if not np.isnan(dc_low) and price < dc_low:
            return ExitSignal("EXIT", f"DC{p} Low 이탈 ({price:,.4f} < {dc_low:,.4f})", price)
        if atr is None:
            a = self._atr(c)
            atr = float(a[-1])
        stop = entry_price - float(self.s["atr_mult"]) * atr
        if price <= stop:
            return ExitSignal("EXIT", f"ATR 손절 ({price:,.4f} ≤ {stop:,.4f})", price)
        return ExitSignal("HOLD", f"보유 유지 (DC{p} Low {dc_low:,.4f} / 손절 {stop:,.4f})", price)


class TradeLimiter:
    """5M 필터 6번: 종목당 일일 최대 3회 + 손절 후 60분 쿨다운."""
    def __init__(self, max_per_day: int = FILTER_DAILY_MAX_TRADES,
                 cooldown_minutes: int = FILTER_COOLDOWN_MINUTES):
        self.max_per_day = max_per_day
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self._entries: Dict[str, Deque[datetime]] = defaultdict(deque)
        self._cooldown_until: Dict[str, datetime] = {}

    def _prune(self, ticker: str, now: datetime) -> None:
        q = self._entries[ticker]
        while q and (now - q[0]) > timedelta(days=1):
            q.popleft()

    def can_enter(self, ticker: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        self._prune(ticker, now)
        until = self._cooldown_until.get(ticker)
        if until and now < until:
            return False
        return len(self._entries[ticker]) < self.max_per_day

    def record_entry(self, ticker: str, now: Optional[datetime] = None) -> None:
        self._entries[ticker].append(now or datetime.now(timezone.utc))

    def record_stop(self, ticker: str, now: Optional[datetime] = None) -> None:
        self._cooldown_until[ticker] = (now or datetime.now(timezone.utc)) + self.cooldown


if __name__ == "__main__":
    import datastore
    root = Path(__file__).resolve().parent.parent
    db = str(root / "data" / "upbit.db")
    strat = TurtleMTF(SLEEVE_1D)
    c = datastore.load(db, "1d", "KRW-BTC")
    if len(c) < 60:
        print("DB 데이터 없음 — data_crawler.py 먼저 실행 (또는 툴로 기존 DB 임포트)")
        raise SystemExit(0)
    sig = strat.check_entry(c)
    print(f"KRW-BTC 1D 진입신호: {sig.signal}  {sig.reason}")
    print(f"  ATR={sig.atr:,.2f}  DC55 High={sig.dc_high:,.2f}  현재가={sig.price:,.2f}")
    sz = calc_position_size(1_000_000, sig.atr, sig.price, SLEEVE_1D["risk_pct"])
    print(f"  유닛 사이즈(자본 100만원, risk 1.0%) = {sz:,} KRW")
    ex = strat.check_exit(c, entry_price=sig.price - 3 * sig.atr)
    print(f"  청산신호: {ex.signal}  {ex.reason}")
