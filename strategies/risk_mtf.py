# -*- coding: utf-8 -*-
"""멀티TF 리스크 매니저 (원본 strategies/risk_mtf.py + CTP v3).

보유 포지션 하나에 대해 매 사이클 우선순위로 '첫 매치만' 실행:
  1) 💀 Fatal Stop      price ≤ entry - 3×ATR                       → SELL_ALL
  2) 〽️ CTP 사다리      peak_gain ≥ 3% 에서 4단 사다리 설치, 아래로 pop → SELL_PARTIAL
                        (마지막 단은 avg+2% 이상일 때만 체결, 아니면 재삽입 후 HOLD)
  3) 🔥 불타기          price ≥ last_buy + 0.5×ATR (max_pyramid, CTP 중 차단) → BUY_UP
  4) 💧 물타기          price ≤ entry - (units×0.7+0.7)×ATR (평단 -5% 차단)   → BUY_DOWN
  5) ⏸️ HOLD

CTP 사다리 (원본 DTB v2 / BaekdooCoin CTP 이식본과 동일)
  1차: peak×0.99/0.97/0.95/0.93 → 10% / 20% / 50% / 100%(잔량)
  N차: peak×0.99/0.975/0.96/0.94 → 10% / 10% / 20% / 60%(잔량)
  ratio ≥ 0.5 인 마지막 단은 (price ≥ avg × 1.02) 일 때만 체결
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("baekdoocoin.risk")

# ---------------- 캡 ----------------
CAP_1D_SLEEVE = 0.06
CAP_5M_SLEEVE = 0.015
CAP_PORTFOLIO = 0.07
GRAD_MAX_POSITION_RISK = 0.025

# ---------------- CTP ----------------
CTP_ACTIVATE_PCT = 3.0
CTP_MIN_PROFIT_PCT = 2.0
CTP_LADDER_1 = [(0.99, 0.10), (0.97, 0.20), (0.95, 0.50), (0.93, 1.00)]
CTP_LADDER_N = [(0.99, 0.10), (0.975, 0.10), (0.96, 0.20), (0.94, 0.60)]

# ---------------- 피라미딩/물타기 ----------------
ATR_UP = 0.5
ATR_DOWN = 0.7
MAX_EXTRA_DOWN = 2
WATER_FLOOR_PCT = -5.0


@dataclass
class SleevePosition:
    ticker: str
    sleeve: str                     # "1D" / "5M"
    strategy: str
    entry_price: float
    atr: float
    amount_krw: float               # 총 투자 원금(평가 아님)
    units: int = 1                  # 보유 유닛 수
    unit_krw: float = 0.0           # 1유닛 금액
    last_buy_price: float = 0.0
    highest_price: float = 0.0
    risk_pct: float = 0.0           # 계좌 대비 리스크 비중
    max_pyramid: int = 2
    fire_count: int = 0
    stage: int = 1
    # CTP 상태
    ctp_active: bool = False
    ctp_ladder: List[List[float]] = field(default_factory=list)   # [[trigger, ratio, step], ...]
    ctp_ladder_peak: float = 0.0
    ctp_ladder_gen: int = 0
    ctp_pending: Optional[List[float]] = None                     # 체결 대기 중인 rung
    # 청산 통계
    exit_stage: int = 0
    realized_krw: float = 0.0
    opened_at: str = ""
    auto_manage: bool = True          # False = 수동/거래소동기화 포지션(관망 전용)
    extras: Dict[str, float] = field(default_factory=dict)

    # ---- helpers ----
    @property
    def avg_price(self) -> float:
        """평단가 = 원금 / 수량. amount_krw 는 매수 원금 누적."""
        qty = self.extras.get("qty", 0.0)
        if qty > 0:
            return self.amount_krw / qty
        return self.entry_price

    def peak_gain_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (price / self.avg_price - 1.0) * 100.0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["avg_price"] = self.avg_price
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SleevePosition":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        pos = cls(**d)
        return pos


@dataclass
class Action:
    kind: str          # SELL_ALL | SELL_PARTIAL | BUY_UP | BUY_DOWN | HOLD
    ticker: str
    ratio: float = 0.0        # 청산 비율(잔량 기준)
    amount_krw: float = 0.0   # 매수 금액
    reason: str = ""


class MTFRiskManager:
    def __init__(self, equity_krw: float = 1_000_000.0,
                 cap_1d: float = CAP_1D_SLEEVE, cap_5m: float = CAP_5M_SLEEVE,
                 cap_total: float = CAP_PORTFOLIO,
                 ctp_activate_pct: float = CTP_ACTIVATE_PCT,
                 ctp_min_profit_pct: float = CTP_MIN_PROFIT_PCT):
        self.equity_krw = float(equity_krw)
        self.cap_1d = cap_1d
        self.cap_5m = cap_5m
        self.cap_total = cap_total
        self.ctp_activate_pct = ctp_activate_pct
        self.ctp_min_profit_pct = ctp_min_profit_pct
        self.positions: Dict[str, SleevePosition] = {}

    # ---------------- 조회 ----------------
    def get(self, ticker: str) -> Optional[SleevePosition]:
        return self.positions.get(ticker)

    def sleeve_risk(self, sleeve: str) -> float:
        return sum(p.risk_pct for p in self.positions.values() if p.sleeve == sleeve)

    def total_risk(self) -> float:
        return sum(p.risk_pct for p in self.positions.values())

    def count_in(self, sleeve: str) -> int:
        return sum(1 for p in self.positions.values() if p.sleeve == sleeve)

    # ---------------- 진입 승인 ----------------
    def approve_entry(self, ticker: str, sleeve: str, requested_krw: float,
                      price: float, atr: float) -> tuple[float, str]:
        """반환 (승인 금액, 사유). 0 이면 거부."""
        if ticker in self.positions:
            return 0.0, "이미 포지션 보유"
        max_stocks = 5 if sleeve == "1D" else 3
        if self.count_in(sleeve) >= max_stocks:
            return 0.0, f"{sleeve} 슬리브 종목수 초과({max_stocks})"
        if price <= 0 or atr <= 0:
            return 0.0, "가격/ATR 무효"

        sleeve_cap = self.cap_1d if sleeve == "1D" else self.cap_5m
        # 진입 리스크 = (2×ATR 손절까지의 %) × 투입비중
        stop_pct = (2.0 * atr) / price
        req_risk = stop_pct * (requested_krw / max(self.equity_krw, 1.0))

        approved = requested_krw
        reason = "OK"

        # 슬리브 캡: 비례 축소
        room = sleeve_cap - self.sleeve_risk(sleeve)
        if req_risk > room:
            if room <= 0:
                return 0.0, f"{sleeve} 슬리브 리스크캡 소진({sleeve_cap:.1%})"
            approved *= room / req_risk
            req_risk = room
            reason = f"{sleeve} 캡 축소"

        # 포트폴리오 총캡: 비례 축소
        room_t = self.cap_total - self.total_risk()
        if req_risk > room_t:
            if room_t <= 0:
                return 0.0, f"포트폴리오 총캡 소진({self.cap_total:.1%})"
            approved *= room_t / req_risk
            req_risk = room_t
            reason = "포트폴리오 캡 축소"

        if approved < 5_000:
            return 0.0, f"승인금액 최소주문 미달({approved:,.0f} KRW)"
        return float(int(approved)), f"{reason} (risk {req_risk:.2%})"

    def register(self, pos: SleevePosition, qty: float = 0.0) -> None:
        if qty > 0:
            pos.extras["qty"] = qty
        elif pos.entry_price > 0 and "qty" not in pos.extras:
            pos.extras["qty"] = pos.amount_krw / pos.entry_price
        self.positions[pos.ticker] = pos

    def remove(self, ticker: str) -> Optional[SleevePosition]:
        return self.positions.pop(ticker, None)

    # ---------------- 승급 ----------------
    def approve_graduation(self, ticker: str, price: float, atr_5m: float) -> bool:
        p = self.positions.get(ticker)
        if not p or p.sleeve != "5M":
            return False
        if p.avg_price <= 0 or atr_5m <= 0:
            return False
        if (price - p.avg_price) < 1.5 * atr_5m:
            return False
        if p.risk_pct > GRAD_MAX_POSITION_RISK:
            return False
        if self.sleeve_risk("1D") + p.risk_pct > self.cap_1d:
            return False
        if self.total_risk() > self.cap_total:
            return False
        return True

    def graduate(self, ticker: str) -> None:
        p = self.positions.get(ticker)
        if p:
            p.sleeve = "1D"
            p.max_pyramid = 2

    # ---------------- 사이클 평가 ----------------
    def evaluate(self, ticker: str, price: float, atr: Optional[float] = None) -> Action:
        p = self.positions.get(ticker)
        if not p:
            return Action("HOLD", ticker, reason="포지션 없음")
        if atr is None or atr <= 0:
            atr = p.atr
        if atr <= 0 or price <= 0:
            return Action("HOLD", ticker, reason="가격/ATR 무효")

        p.highest_price = max(p.highest_price, price)
        avg = p.avg_price

        # 1) Fatal Stop
        stop = p.entry_price - 3.0 * p.atr
        if price <= stop:
            return Action("SELL_ALL", ticker, ratio=1.0,
                          reason=f"Fatal Stop ({price:,.4f} ≤ {stop:,.4f})")

        # 2) CTP
        act = self._ctp(p, price)
        if act is not None:
            return act

        # 3) 불타기
        ref = p.last_buy_price or p.entry_price
        if (not p.ctp_active and p.fire_count < p.max_pyramid and price >= ref + ATR_UP * atr):
            return Action("BUY_UP", ticker, amount_krw=p.unit_krw or p.amount_krw,
                          reason=f"불타기 #{p.fire_count + 1} ({price:,.4f} ≥ {ref + ATR_UP * atr:,.4f})")

        # 4) 물타기
        max_units = p.max_pyramid + MAX_EXTRA_DOWN
        trig = p.entry_price - ((p.units * ATR_DOWN) + ATR_DOWN) * atr
        if p.units < max_units and price <= trig:
            if avg > 0 and price <= avg * (1 + WATER_FLOOR_PCT / 100.0):
                return Action("HOLD", ticker,
                              reason=f"물타기 차단 (평단 대비 {WATER_FLOOR_PCT:.0f}% 이하)")
            return Action("BUY_DOWN", ticker, amount_krw=p.unit_krw or p.amount_krw,
                          reason=f"물타기 Stage {p.units + 1} ({price:,.4f} ≤ {trig:,.4f})")

        return Action("HOLD", ticker,
                      reason=f"보유 (peak {p.peak_gain_pct(p.highest_price):+.2f}%, "
                             f"CTP {'ON' if p.ctp_active else 'OFF'})")

    # ---------------- CTP 내부 ----------------
    def _install_ladder(self, p: SleevePosition, peak: float) -> None:
        p.ctp_ladder_gen += 1
        spec = CTP_LADDER_1 if p.ctp_ladder_gen == 1 else CTP_LADDER_N
        min_price = p.avg_price * (1 + self.ctp_min_profit_pct / 100.0)
        ladder: List[List[float]] = []
        for i, (mult, ratio) in enumerate(spec):
            trig = peak * mult
            # 고비중 rung(ratio>=0.5)의 트리거가 평단+2% 방어선보다 아래면 영구 도달 불가
            # → 트리거를 방어선으로 끌어올려 '사다리는 항상 소진된다'를 불변식으로 만든다.
            if ratio >= 0.5:
                trig = max(trig, min_price)
            ladder.append([trig, ratio, i + 1])
        p.ctp_ladder = sorted(ladder, key=lambda x: x[0], reverse=True)
        p.ctp_ladder_peak = peak
        p.ctp_active = True
        p.ctp_pending = None
        log.info("〽️ CTP 사다리 설치 %s gen=%d peak=%.4f 비율=%s",
                 p.ticker, p.ctp_ladder_gen, peak, [r for _, r, _ in p.ctp_ladder])

    def _ctp(self, p: SleevePosition, price: float) -> Optional[Action]:
        peak = p.highest_price
        if peak <= 0 or p.avg_price <= 0:
            return None

        gain_pct = (peak / p.avg_price - 1.0) * 100.0

        # 새 고점 갱신 → 사다리 재설치 ("추세 쫓아가기")
        if p.ctp_active and peak > p.ctp_ladder_peak:
            self._install_ladder(p, peak)
            return Action("HOLD", p.ticker, reason=f"〽️ 새 고점 → 사다리 재설치 (peak {peak:,.4f})")

        # 발동
        if not p.ctp_active:
            if gain_pct >= self.ctp_activate_pct:
                self._install_ladder(p, peak)
                return Action("HOLD", p.ticker,
                              reason=f"〽️ CTP 발동 (peak {gain_pct:+.2f}%, 사다리 4단 설치)")
            return None

        # pop: 가격이 trigger 아래로 내려온 첫 단(사다리는 높은 가격 → 낮은 가격 순)
        if not p.ctp_ladder:
            return None
        trigger, ratio, step = p.ctp_ladder[0]
        if price > trigger:
            return None

        # 마지막 단 방어: avg + 2% 미만이면 차단 → HOLD (rung 은 소비하지 않는다)
        min_price = p.avg_price * (1 + self.ctp_min_profit_pct / 100.0)
        if ratio >= 0.5 and price < min_price:
            return Action("HOLD", p.ticker,
                          reason=f"〽️ CTP step{step} 차단 (avg+{self.ctp_min_profit_pct:.0f}% = "
                                 f"{min_price:,.4f} > {price:,.4f})")

        # ⚠️ rung 소비는 '체결 확정(on_sell)'에서 한다 — 주문이 실패해도 사다리가 유실되지 않도록.
        p.ctp_pending = [trigger, ratio, step]
        return Action("SELL_PARTIAL", p.ticker, ratio=min(ratio, 1.0),
                      reason=f"〽️ CTP step{step}/4 ({price:,.4f} ≤ {trigger:,.4f}) {ratio:.0%} "
                             f"[avg{'+' if price >= p.avg_price else ''}{(price / p.avg_price - 1) * 100:.1f}%]")

    # ---------------- 체결 반영 ----------------
    def _recalc_risk(self, p: SleevePosition) -> None:
        """리스크는 '현재' 보유수량 기준으로 갱신한다 (캡이 실제 노출을 추적하도록)."""
        qty = float(p.extras.get("qty", 0.0))
        p.risk_pct = (2.0 * p.atr * qty) / max(self.equity_krw, 1.0)

    def on_buy(self, ticker: str, price: float, amount_krw: float, kind: str = "BUY_UP") -> None:
        p = self.positions.get(ticker)
        if not p:
            return
        qty_add = amount_krw / price if price > 0 else 0.0
        p.extras["qty"] = p.extras.get("qty", 0.0) + qty_add
        p.amount_krw += amount_krw
        p.last_buy_price = price
        p.units += 1
        if kind == "BUY_UP":
            p.fire_count += 1
        else:
            p.stage += 1
        self._recalc_risk(p)
        if p.risk_pct > GRAD_MAX_POSITION_RISK:
            log.warning("⚠️ %s 종목 리스크 %.2f%% > 상한 %.1f%%",
                        ticker, p.risk_pct * 100, GRAD_MAX_POSITION_RISK * 100)

    def on_sell(self, ticker: str, price: float, ratio: float) -> Optional[SleevePosition]:
        """부분/전량 매도 반영. 전량이면 포지션 제거 후 반환."""
        p = self.positions.get(ticker)
        if not p:
            return None
        # 체결 확정 → 대기 중이던 CTP rung 소비
        if getattr(p, "ctp_pending", None):
            trig = p.ctp_pending[0]
            if p.ctp_ladder and abs(p.ctp_ladder[0][0] - trig) < 1e-9:
                p.ctp_ladder.pop(0)
            p.ctp_pending = None
        qty = p.extras.get("qty", 0.0)
        sell_qty = qty * min(max(ratio, 0.0), 1.0)
        avg = p.avg_price
        p.realized_krw += (price - avg) * sell_qty
        p.extras["qty"] = max(qty - sell_qty, 0.0)
        p.amount_krw = max(p.amount_krw - avg * sell_qty, 0.0)
        p.exit_stage += 1
        if p.extras["qty"] <= 1e-12 or ratio >= 1.0:
            return self.remove(ticker)
        self._recalc_risk(p)
        p.units = max(int(round(p.extras["qty"] / max(p.unit_krw / avg, 1e-12))) if p.unit_krw > 0 else 1, 1)
        return p

    # ---------------- 영속화 ----------------
    def to_json(self) -> dict:
        return {t: p.to_dict() for t, p in self.positions.items()}

    def load_json(self, data: dict) -> None:
        for t, d in (data or {}).items():
            try:
                self.positions[t] = SleevePosition.from_dict(d)
            except Exception as e:                 # noqa: BLE001
                log.warning("포지션 복구 실패 %s: %s", t, e)


if __name__ == "__main__":
    """자체 테스트 (원본 CTP 셀프테스트 6종 대응)."""
    rm = MTFRiskManager(equity_krw=10_000_000)
    rm.register(SleevePosition(ticker="KRW-TEST", sleeve="1D", strategy="turtle",
                               entry_price=100_000, atr=1_000, amount_krw=1_000_000,
                               unit_krw=500_000, last_buy_price=100_000, highest_price=100_000,
                               risk_pct=0.01, max_pyramid=2), qty=10.0)
    a = rm.evaluate("KRW-TEST", 100_400)      # +0.4% → 트리거 미달
    print("TEST1", a.kind, "|", a.reason)
    assert a.kind == "HOLD"
    a = rm.evaluate("KRW-TEST", 103_500)      # +3.5% → CTP 발동
    print("TEST2", a.kind, "|", a.reason)
    p = rm.get("KRW-TEST")
    print("TEST2 사다리", [[round(t, 1), r, s] for t, r, s in p.ctp_ladder])
    assert p.ctp_active and len(p.ctp_ladder) == 4

    a = rm.evaluate("KRW-TEST", 102_400)      # step1 trigger(peak×0.99=102,465) 아래 → pop
    print("TEST3", a.kind, f"{a.ratio:.0%}", "|", a.reason)
    assert a.kind == "SELL_PARTIAL" and abs(a.ratio - 0.10) < 1e-9
    rm.on_sell("KRW-TEST", 102_400, a.ratio)

    a = rm.evaluate("KRW-TEST", 106_000)      # 새 고점 → 사다리 재설치
    print("TEST4", a.kind, "|", a.reason)
    p = rm.get("KRW-TEST")
    assert p.ctp_ladder_gen == 2 and len(p.ctp_ladder) == 4

    a = rm.evaluate("KRW-TEST", 104_800)      # gen2 step1 pop (10%)
    print("TEST5", a.kind, f"{a.ratio:.0%}", "|", a.reason)
    assert a.kind == "SELL_PARTIAL" and abs(a.ratio - 0.10) < 1e-9
    rm.on_sell("KRW-TEST", 104_800, a.ratio)

    # 마지막 단 방어: 사다리를 끝까지 남긴 뒤 avg+2% 미만 가격
    p = rm.get("KRW-TEST")
    p.ctp_ladder = [[p.avg_price * 1.01, 0.6, 4]]
    a = rm.evaluate("KRW-TEST", p.avg_price * 1.005)
    print("TEST6", a.kind, "|", a.reason)
    assert a.kind == "HOLD" and "차단" in a.reason

    # 물타기 차단 (-5% 이하) — ATR을 키워 손절선(3×ATR)과 물타기 하한 사이 구간을 만든다
    rm2 = MTFRiskManager(equity_krw=10_000_000)
    rm2.register(SleevePosition(ticker="KRW-T2", sleeve="5M", strategy="turtle", entry_price=100_000,
                                atr=2_000, amount_krw=500_000, unit_krw=500_000,
                                last_buy_price=100_000, highest_price=100_000, risk_pct=0.003,
                                max_pyramid=1), qty=5.0)
    a = rm2.evaluate("KRW-T2", 94_500)     # 물타기 트리거(94,400) 근처 & 평단-5%(95,000) 이하
    print("TEST7", a.kind, "|", a.reason)
    assert a.kind == "HOLD" and "물타기 차단" in a.reason

    # Fatal Stop (entry - 3×ATR = 94,000)
    a = rm2.evaluate("KRW-T2", 93_900)
    print("TEST8", a.kind, "|", a.reason)
    assert a.kind == "SELL_ALL"
    print("\nALL RISK/CTP TESTS PASSED (8/8)")
