"""
백두코인봇 — Multi-TF Portfolio Risk Manager
============================================

1D + 5M 듀얼 슬리브 포트폴리오 리스크 관리.

주요 기능:
  1. 슬리브별 리스크캡: 1D ≤ 6%, 5M ≤ 1.5%
  2. 포트폴리오 총캡: ≤ 7%
  3. Graduation (승급): 5M→1D 승급 시 리스크 재계산
  4. 포지션 사이즈 자동 조정 (슬리브캡 초과 시 축소)
  5. 신규 진입 승인/거부 (캡 초과 시 거부)

사용법:
    from risk_mtf import MTFRiskManager
    rm = MTFRiskManager()
    
    # 신규 진입 요청
    approved, size = rm.approve_entry("5M", ticker, requested_size, risk_pct)
    
    # Graduation
    approved, new_size = rm.approve_graduation(ticker, profit_atr, current_risk)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SleevePosition:
    """개별 슬리브 내 포지션."""
    ticker: str
    entry_price: float
    atr: float
    amount_krw: int
    risk_pct: float           # portfolio % at risk for this position
    sleeve: str               # "1D" | "5M"
    units: int = 1
    highest_price: float = 0.0
    fire_count: int = 0
    ctp_active: bool = False
    ctp_ladder: List = field(default_factory=list)      # [[trigger_price, ratio, step], ...]
    # ---- CTP (Cascade Take-Profit) 상태 ----
    avg_price: float = 0.0          # 평단(증액 시 갱신). 0 이면 entry_price 로 간주
    ctp_ladder_peak: float = 0.0    # 사다리를 설치한 고점
    ctp_ladder_gen: int = 0         # 사다리 세대(1차/재설치)
    pending_rung: Optional[List] = None   # 체결 대기 중인 rung(주문 실패 시 복원)
    realized_krw: float = 0.0       # 누적 실현손익

    @property
    def current_value_krw(self) -> int:
        return self.amount_krw

    @property
    def effective_avg_price(self) -> float:
        return self.avg_price if self.avg_price > 0 else self.entry_price


@dataclass
class PortfolioSnapshot:
    """포트폴리오 전체 스냅샷."""
    equity_krw: float
    total_risk_pct: float     # portfolio total risk %
    sleeve_1d_risk_pct: float
    sleeve_5m_risk_pct: float
    positions_1d: int
    positions_5m: int
    available_1d_risk_pct: float  # remaining 1D capacity
    available_5m_risk_pct: float  # remaining 5M capacity
    total_committed_krw: int


class MTFRiskManager:
    """멀티TF 포트폴리오 리스크 매니저."""

    # Sleeve caps
    CAP_1D_SLEEVE = 0.06       # 6%
    CAP_5M_SLEEVE = 0.015      # 1.5%
    CAP_PORTFOLIO = 0.07       # 7% total

    # Graduation limits
    GRAD_MAX_POSITION_RISK = 0.025  # single position max risk post-graduation

    def __init__(self, equity_krw: float = 0):
        self.equity_krw = equity_krw
        self._positions: Dict[str, SleevePosition] = {}
        # {ticker: SleevePosition}

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------
    def add_position(self, ticker: str, sleeve: str, entry_price: float,
                     atr: float, amount_krw: int, risk_pct: float,
                     units: int = 1) -> bool:
        """Add a new position. Returns False if ticker already has a position."""
        if ticker in self._positions:
            logger.warning("Position already exists for %s", ticker)
            return False
        self._positions[ticker] = SleevePosition(
            ticker=ticker, sleeve=sleeve, entry_price=entry_price,
            atr=atr, amount_krw=amount_krw, risk_pct=risk_pct,
            units=units, highest_price=entry_price, avg_price=entry_price,
        )
        logger.info("Position added: %s [%s] ₩%d risk %.2f%%",
                    ticker, sleeve, amount_krw, risk_pct * 100)
        return True

    def remove_position(self, ticker: str) -> Optional[SleevePosition]:
        """Remove a position. Returns the removed position or None."""
        return self._positions.pop(ticker, None)

    def update_position(self, ticker: str, **kwargs) -> bool:
        """Update position fields (amount_krw, entry_price, units, highest_price, etc.)."""
        pos = self._positions.get(ticker)
        if pos is None:
            logger.warning("Cannot update non-existent position: %s", ticker)
            return False
        for k, v in kwargs.items():
            if hasattr(pos, k):
                setattr(pos, k, v)
        return True

    # ------------------------------------------------------------------
    # CTP (Cascade Take-Profit) — 계단식 분할 익절
    # ------------------------------------------------------------------
    # 원본 strategies/risk.py 의 CTP 를 MTF 리스크매니저로 이식.
    # MTF 경로는 포지션을 금액(amount_krw)으로 추적하므로 매도 비율도 금액 비율로 계산한다.
    CTP_ACTIVATE_PCT = 3.0       # 고점 수익률이 이 값을 넘으면 사다리 설치
    CTP_MIN_PROFIT_PCT = 2.0     # 마지막 rung 은 평단 +2% 이상일 때만 체결
    CTP_LADDER_1 = ((0.99, 0.10), (0.97, 0.20), (0.95, 0.50), (0.93, 1.00))
    CTP_LADDER_N = ((0.99, 0.10), (0.975, 0.10), (0.96, 0.20), (0.94, 0.60))

    def record_add(self, ticker: str, price: float, add_krw: int) -> None:
        """불타기/물타기 증액 시 평단을 갱신한다(CTP 발동선이 실제 평단을 따라가도록)."""
        pos = self._positions.get(ticker)
        if pos is None or price <= 0 or add_krw <= 0:
            return
        avg = pos.effective_avg_price
        # 금액 기준 가중평균 (수량을 따로 저장하지 않는 MTF 모델에 맞춤)
        new_avg = (avg * pos.amount_krw + price * add_krw) / max(pos.amount_krw + add_krw, 1)
        pos.avg_price = new_avg
        pos.amount_krw += int(add_krw)

    def _install_ladder(self, pos: SleevePosition, peak: float) -> None:
        pos.ctp_ladder_gen += 1
        spec = self.CTP_LADDER_1 if pos.ctp_ladder_gen == 1 else self.CTP_LADDER_N
        avg = pos.effective_avg_price
        min_price = avg * (1 + self.CTP_MIN_PROFIT_PCT / 100.0)
        ladder = []
        for step, (mult, ratio) in enumerate(spec, start=1):
            trig = peak * mult
            if ratio >= 0.5:
                # 트리거가 방어선(평단+2%)보다 아래면 영구 도달 불가 → 방어선으로 끌어올린다
                trig = max(trig, min_price)
            ladder.append([trig, ratio, step])
        pos.ctp_ladder = sorted(ladder, key=lambda x: x[0], reverse=True)
        pos.ctp_ladder_peak = peak
        pos.ctp_active = True
        pos.pending_rung = None
        logger.info("〽️ CTP 사다리 설치 %s gen=%d peak=%.2f avg=%.2f rungs=%s",
                    pos.ticker, pos.ctp_ladder_gen, peak, avg,
                    [(round(t, 2), r) for t, r, _ in pos.ctp_ladder])

    def evaluate_ctp(self, ticker: str, price: float) -> Optional[dict]:
        """CTP 판정. 반환: None(해당 없음) | {"action": "HOLD"|"SELL_PARTIAL", ...}.

        · 고점 수익률 ≥ CTP_ACTIVATE_PCT → 4단 사다리 설치(HOLD)
        · 가격이 최상단 rung 이하로 내려오면 그 rung 을 '체결 대기'로 표시하고 SELL_PARTIAL
        · rung 소비는 체결 확정 시(apply_partial_sell)에만 — 주문 실패 시 restore_pending
        """
        pos = self._positions.get(ticker)
        if pos is None or price <= 0:
            return None
        pos.highest_price = max(pos.highest_price, price)
        avg = pos.effective_avg_price
        if avg <= 0:
            return None

        peak_gain_pct = (pos.highest_price / avg - 1.0) * 100.0

        # 새 고점이면 사다리 재설치(추세 추종)
        if pos.ctp_active and pos.highest_price > pos.ctp_ladder_peak + 1e-9:
            self._install_ladder(pos, pos.highest_price)
            return {"action": "HOLD",
                    "reason": f"〽️ CTP 새 고점({peak_gain_pct:+.2f}%) → 사다리 재설치 gen={pos.ctp_ladder_gen}"}

        if not pos.ctp_active:
            if peak_gain_pct >= self.CTP_ACTIVATE_PCT:
                self._install_ladder(pos, pos.highest_price)
                return {"action": "HOLD",
                        "reason": f"〽️ CTP 발동(고점 {peak_gain_pct:+.2f}%) — 사다리 4단 설치"}
            return None

        if not pos.ctp_ladder:
            return None
        trigger, ratio, step = pos.ctp_ladder[0]
        if price > trigger:
            return None

        min_price = avg * (1 + self.CTP_MIN_PROFIT_PCT / 100.0)
        if ratio >= 0.5 and price < min_price:
            return {"action": "HOLD",
                    "reason": (f"〽️ CTP step{step} 보류 — 평단+{self.CTP_MIN_PROFIT_PCT:.0f}%"
                               f"({min_price:,.0f}) 미달 (현재 {price:,.0f})")}

        pos.pending_rung = [trigger, ratio, step]
        return {"action": "SELL_PARTIAL", "ratio": ratio, "step": step,
                "reason": (f"〽️ CTP step{step}/4 {price:,.0f} ≤ {trigger:,.0f}"
                           f" → {ratio:.0%} 익절 (평단대비 {(price / avg - 1) * 100:+.2f}%)")}

    def apply_partial_sell(self, ticker: str, price: float, ratio: float) -> int:
        """부분 익절 체결 반영: rung 소비 + 금액/실현손익 갱신. 반환=남은 amount_krw."""
        pos = self._positions.get(ticker)
        if pos is None:
            return 0
        if pos.pending_rung:
            trig = pos.pending_rung[0]
            if pos.ctp_ladder and abs(pos.ctp_ladder[0][0] - trig) < 1e-9:
                pos.ctp_ladder.pop(0)
            pos.pending_rung = None
        ratio = min(max(ratio, 0.0), 1.0)
        sold_krw = int(pos.amount_krw * ratio)
        avg = pos.effective_avg_price
        if avg > 0 and sold_krw > 0:
            # 금액 기준 손익 근사: (체결가/평단 - 1) × 매도금액
            pos.realized_krw += (price / avg - 1.0) * sold_krw
        pos.amount_krw = max(pos.amount_krw - sold_krw, 0)
        logger.info("〽️ CTP 부분익절 %s ₩%d (%.0f%%) 남은 ₩%d 실현 %.0f",
                    ticker, sold_krw, ratio * 100, pos.amount_krw, pos.realized_krw)
        if pos.amount_krw <= 0 or ratio >= 1.0:
            self.remove_position(ticker)
        return pos.amount_krw

    def restore_pending(self, ticker: str) -> bool:
        """주문 실패 시 체결 대기 rung 을 사다리에 되돌린다(사다리 유실 방지)."""
        pos = self._positions.get(ticker)
        if pos is None or not pos.pending_rung:
            return False
        pos.pending_rung = None
        logger.warning("〽️ CTP rung 복원 %s (주문 실패)", ticker)
        return True

    def get_position(self, ticker: str) -> Optional[SleevePosition]:
        return self._positions.get(ticker)

    def get_positions_by_sleeve(self, sleeve: str) -> List[SleevePosition]:
        return [p for p in self._positions.values() if p.sleeve == sleeve]

    def get_all_positions(self) -> List[SleevePosition]:
        return list(self._positions.values())

    @property
    def total_positions(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------
    # Risk metrics
    # ------------------------------------------------------------------
    def sleeve_risk(self, sleeve: str) -> float:
        """Total risk for a given sleeve (% of portfolio)."""
        return sum(p.risk_pct for p in self._positions.values()
                  if p.sleeve == sleeve)

    def total_risk(self) -> float:
        """Total portfolio risk (% of portfolio)."""
        return sum(p.risk_pct for p in self._positions.values())

    def sleeve_count(self, sleeve: str) -> int:
        return sum(1 for p in self._positions.values() if p.sleeve == sleeve)

    # ------------------------------------------------------------------
    # Entry approval
    # ------------------------------------------------------------------
    def approve_entry(self, sleeve: str, ticker: str, requested_size_krw: int,
                      risk_pct_per_position: float) -> Tuple[bool, int, str]:
        """Approve or reject a new entry.

        Parameters
        ----------
        sleeve : "1D" | "5M"
        ticker : str
        requested_size_krw : int
            Requested position size in KRW.
        risk_pct_per_position : float
            Risk as % of equity for this single position.

        Returns
        -------
        (approved: bool, adjusted_size_krw: int, reason: str)
        """
        # Check ticker already open
        if ticker in self._positions:
            return (False, 0, f"{ticker} already has open position")
        if self.equity_krw <= 0:
            return (False, 0, "Equity is zero")

        sleeve_cap = self.CAP_1D_SLEEVE if sleeve == "1D" else self.CAP_5M_SLEEVE
        max_stocks = 5 if sleeve == "1D" else 3

        # Check position count
        current_count = self.sleeve_count(sleeve)
        if current_count >= max_stocks:
            return (False, 0, f"{sleeve} sleeve full ({current_count}/{max_stocks})")

        # Check sleeve cap
        current_sleeve_risk = self.sleeve_risk(sleeve)
        new_sleeve_risk = current_sleeve_risk + risk_pct_per_position
        if new_sleeve_risk > sleeve_cap:
            # Try to scale down
            available = sleeve_cap - current_sleeve_risk
            if available <= 0:
                return (False, 0,
                    f"{sleeve} sleeve risk cap reached: {current_sleeve_risk*100:.1f}% ≥ {sleeve_cap*100:.0f}%")
            # Scale the position down proportionally
            adjusted = risk_pct_per_position / available * requested_size_krw
            adjusted = max(int(adjusted), 5_000)
            new_risk = current_sleeve_risk + available
            logger.info("%s sleeve cap: scaling %s from ₩%d → ₩%d (risk %.3f%%→%.3f%%)",
                        sleeve, ticker, requested_size_krw, adjusted,
                        risk_pct_per_position * 100, available * 100)
            requested_size_krw = adjusted
            risk_pct_per_position = available
            new_sleeve_risk = new_risk

        # Check portfolio total cap
        current_total = self.total_risk()
        new_total = current_total + risk_pct_per_position
        if new_total > self.CAP_PORTFOLIO:
            available_total = self.CAP_PORTFOLIO - current_total
            if available_total <= 0:
                return (False, 0,
                    f"Portfolio risk cap reached: {current_total*100:.1f}% ≥ {self.CAP_PORTFOLIO*100:.0f}%")
            # Scale down
            adjusted = int(requested_size_krw * available_total / risk_pct_per_position)
            adjusted = max(adjusted, 5_000)
            logger.info("Portfolio cap: scaling %s from ₩%d → ₩%d", ticker, requested_size_krw, adjusted)
            requested_size_krw = adjusted
            risk_pct_per_position = available_total

        return (True, requested_size_krw,
                f"Approved: {sleeve} sleeve={new_sleeve_risk*100:.1f}%/{sleeve_cap*100:.0f}%, "
                f"portfolio={new_total*100:.1f}%/{self.CAP_PORTFOLIO*100:.0f}%")

    # ------------------------------------------------------------------
    # Graduation (승급) logic
    # ------------------------------------------------------------------
    def approve_graduation(self, ticker: str, new_1d_risk_pct: float) -> Tuple[bool, str]:
        """Approve 5M→1D graduation for a position.

        Steps:
        1. Verify 5M position exists
        2. Check 1D sleeve capacity
        3. Check position-level risk ≤ 2.5%
        4. Check portfolio total cap
        """
        pos = self._positions.get(ticker)
        if pos is None:
            return (False, f"No position for {ticker}")
        if pos.sleeve != "5M":
            return (False, f"{ticker} is not a 5M position")

        # Check position-level risk
        if new_1d_risk_pct > self.GRAD_MAX_POSITION_RISK:
            return (False,
                f"Position risk {new_1d_risk_pct*100:.1f}% > {self.GRAD_MAX_POSITION_RISK*100:.1f}% max")

        # Check 1D sleeve cap
        current_1d = self.sleeve_risk("1D")
        new_1d = current_1d - 0 + new_1d_risk_pct  # 5M risk removed, 1D added
        if new_1d > self.CAP_1D_SLEEVE:
            return (False,
                f"1D sleeve cap: {new_1d*100:.1f}% > {self.CAP_1D_SLEEVE*100:.0f}%")

        # Check portfolio total
        current_5m = pos.risk_pct
        new_total = self.total_risk() - current_5m + new_1d_risk_pct
        if new_total > self.CAP_PORTFOLIO:
            return (False,
                f"Portfolio cap: {new_total*100:.1f}% > {self.CAP_PORTFOLIO*100:.0f}%")

        # Execute graduation
        pos.sleeve = "1D"
        pos.risk_pct = new_1d_risk_pct
        logger.info("Graduation: %s 5M→1D, risk %.1f%%→%.1f%%",
                    ticker, current_5m * 100, new_1d_risk_pct * 100)
        return (True,
            f"Graduation approved: {ticker} 5M→1D (risk {new_1d_risk_pct*100:.1f}%)")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> PortfolioSnapshot:
        """Return current portfolio risk snapshot."""
        return PortfolioSnapshot(
            equity_krw=self.equity_krw,
            total_risk_pct=self.total_risk(),
            sleeve_1d_risk_pct=self.sleeve_risk("1D"),
            sleeve_5m_risk_pct=self.sleeve_risk("5M"),
            positions_1d=self.sleeve_count("1D"),
            positions_5m=self.sleeve_count("5M"),
            available_1d_risk_pct=max(0, self.CAP_1D_SLEEVE - self.sleeve_risk("1D")),
            available_5m_risk_pct=max(0, self.CAP_5M_SLEEVE - self.sleeve_risk("5M")),
            total_committed_krw=sum(p.amount_krw for p in self._positions.values()),
        )

    # ------------------------------------------------------------------
    # Status string (for Telegram reports)
    # ------------------------------------------------------------------
    def status_string(self) -> str:
        s = self.snapshot()
        lines = [
            f"📊 Portfolio Risk: {s.total_risk_pct*100:.1f}%/{self.CAP_PORTFOLIO*100:.0f}%",
            f"  1D Sleeve: {s.sleeve_1d_risk_pct*100:.1f}%/{self.CAP_1D_SLEEVE*100:.0f}% ({s.positions_1d} pos)",
            f"  5M Sleeve: {s.sleeve_5m_risk_pct*100:.1f}%/{self.CAP_5M_SLEEVE*100:.0f}% ({s.positions_5m} pos)",
            f"  Committed: ₩{s.total_committed_krw:,} / ₩{s.equity_krw:,.0f}",
        ]
        if s.positions_1d + s.positions_5m > 0:
            lines.append("")
            for p in self._positions.values():
                lines.append(f"  • {p.ticker} [{p.sleeve}] ₩{p.amount_krw:,} risk {p.risk_pct*100:.1f}%")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    rm = MTFRiskManager(equity_krw=10_000_000)

    print("=" * 60)
    print("  🛡️  Multi-TF Risk Manager — Demo")
    print("=" * 60)

    # Test 1: Add 5M positions
    print("\n📥 Adding 5M positions...")
    for ticker in ["KRW-BTC", "KRW-SOL", "KRW-ETH"]:
        ok, size, reason = rm.approve_entry("5M", ticker, 100_000, 0.003)
        if ok:
            rm.add_position(ticker, "5M", 10_000_000, 50_000, size, 0.003)
            print(f"  ✅ {ticker}: ₩{size:,} — {reason}")
        else:
            print(f"  ❌ {ticker}: {reason}")

    # Test 2: Try to add 4th 5M (should fail)
    print("\n📥 Adding 4th 5M (should fail)...")
    ok, size, reason = rm.approve_entry("5M", "KRW-ADA", 100_000, 0.003)
    print(f"  {'✅' if ok else '❌'} KRW-ADA: {reason}")

    # Test 3: Add 1D positions
    print("\n📥 Adding 1D positions...")
    for ticker in ["KRW-BTC", "KRW-DOGE"]:
        ok, size, reason = rm.approve_entry("1D", ticker, 200_000, 0.01)
        if ok:
            rm.add_position(ticker, "1D", 10_000_000, 100_000, size, 0.01)
            print(f"  ✅ {ticker}: ₩{size:,} — {reason}")
        else:
            print(f"  ❌ {ticker}: {reason}")

    # Test 4: Portfolio status
    print(f"\n{rm.status_string()}")

    # Test 5: Graduation
    print("\n📈 Graduation test...")
    ok, reason = rm.approve_graduation("KRW-SOL", 0.015)
    print(f"  {'✅' if ok else '❌'} KRW-SOL 5M→1D: {reason}")
    if ok:
        print(f"\n{rm.status_string()}")

    # Test 6: Portfolio cap test
    print("\n📥 Adding more to fill portfolio cap...")
    for ticker in ["KRW-XRP", "KRW-AVAX", "KRW-DOT"]:
        ok, size, reason = rm.approve_entry("1D", ticker, 200_000, 0.01)
        if ok:
            rm.add_position(ticker, "1D", 10_000_000, 100_000, size, 0.01)
            print(f"  ✅ {ticker}: ₩{size:,}")
        else:
            print(f"  ❌ {ticker}: {reason}")

    print(f"\n{rm.status_string()}")
    print("\n✅ Multi-TF Risk Manager demo complete")