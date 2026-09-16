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
    ctp_ladder: List = field(default_factory=list)

    @property
    def current_value_krw(self) -> int:
        return self.amount_krw


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
            units=units, highest_price=entry_price,
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