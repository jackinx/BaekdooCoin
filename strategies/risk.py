"""
백두코인봇 — Risk Manager v3 (Crypto Spot) — CTP
================================================

v3: CTP (Cascade Take-Profit) — 2026-08-27 DTB v2에서 이식
    고점에서 4단계 매도 사다리 설치 → 순차 익절 + 새 고점 시 리셋

우선순위:
  1. Fatal Stop: entry_price - 3×ATR → SELL_ALL
  2. 〽️ CTP (peak_gain >= ctp_activate_pct 시 발동):
     a. 새 고점 → 사다리 4개 설치 (1차: 10→20→50→100%, N차: 10→10→20→60%)
     b. 가격 내려오면 위에서부터 pop → 순차 SELL_PARTIAL
     c. 마지막 단은 avg+ctp_min_profit_pct 이상일 때만
  3. ATR Stage 1 Trail: highest - 1×ATR → SELL 30% (CTP off 시만)
  4. ATR Stage 2 Trail: highest - 2×ATR → SELL 30% (CTP off 시만)
  5. 물타기: avg_price - 0.5×ATR or -3% (더 높은 쪽) → BUY 1:1.5:2 (최대 3단계)
     ⚠️ 평단 -5% 이하 → 물타기 차단
  6. 불타기: last_buy + 0.5×ATR → BUY 동일 unit_size (최대 3회)
     ⚠️ CTP 발동 중 → 불타기 차단
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RiskAction(str, Enum):
    HOLD          = "HOLD"
    SELL_PARTIAL  = "SELL_PARTIAL"
    SELL_ALL      = "SELL_ALL"
    BUY_DOWN      = "BUY_DOWN"
    BUY_UP        = "BUY_UP"


@dataclass
class RiskDecision:
    action:     RiskAction = RiskAction.HOLD
    amount_krw: int = 0
    reason:     str = ""

    def is_sell(self) -> bool:
        return self.action in (RiskAction.SELL_PARTIAL, RiskAction.SELL_ALL)

    def is_buy(self) -> bool:
        return self.action in (RiskAction.BUY_DOWN, RiskAction.BUY_UP)


class RiskManager:

    def __init__(
        self,
        atr_fatal_mult:   float = 3.0,
        atr_stage1_mult:  float = 1.0,
        atr_stage2_mult:  float = 2.0,
        atr_sell_frac:    float = 0.30,
        # ── CTP (Cascade Take-Profit) ──
        ctp_activate_pct:   float = 3.0,
        ctp_min_profit_pct: float = 2.0,
        # ── 물타기 ──
        water_atr_mult:   float = 0.5,
        water_avg_pct:    float = 3.0,
        water_mults:      tuple = (1.0, 1.5, 2.0),
        water_max_stages: int   = 3,
        water_floor_pct:  float = -5.0,
        # ── 불타기 ──
        fire_atr_mult:  float = 0.5,
        fire_max_count: int   = 3,
    ) -> None:
        self.atr_fatal_mult    = atr_fatal_mult
        self.atr_stage1_mult   = atr_stage1_mult
        self.atr_stage2_mult   = atr_stage2_mult
        self.atr_sell_frac     = atr_sell_frac
        self.ctp_activate_pct   = ctp_activate_pct
        self.ctp_min_profit_pct = ctp_min_profit_pct
        self.water_atr_mult   = water_atr_mult
        self.water_avg_pct    = water_avg_pct
        self.water_mults      = water_mults
        self.water_max_stages = water_max_stages
        self.water_floor_pct  = water_floor_pct
        self.fire_atr_mult  = fire_atr_mult
        self.fire_max_count = fire_max_count

    # ────────────────────────────────────────────────────────────────
    def evaluate(self, position: dict, current_price: float,
                 unit_size_krw: int) -> RiskDecision:
        """Full position evaluation. Returns RiskDecision."""
        p1         = float(position.get("entry_price", 0))
        last_buy   = float(position.get("last_buy_price", current_price))
        atr        = float(position.get("atr", current_price * 0.02))
        stage      = int(position.get("stage", 1))
        fire_count = int(position.get("fire_count", 0))
        highest    = float(position.get("highest_price", current_price))
        amount_krw = float(position.get("amount_krw", 0))
        exit_stage = int(position.get("exit_stage", 0))
        ctp_active = bool(position.get("ctp_active", False))
        # CTP ladder state (persisted on dict)
        ctp_ladder: list = position.setdefault("ctp_ladder", [])
        ctp_ladder_peak = float(position.get("ctp_ladder_peak", 0.0))
        ctp_ladder_gen = int(position.get("ctp_ladder_gen", 0))

        if p1 <= 0 or current_price <= 0:
            return RiskDecision(RiskAction.HOLD, reason="price=0")

        # 최고가 업데이트
        new_peak = current_price > highest + 1e-8
        if new_peak:
            highest = current_price
            position["highest_price"] = highest

        peak_gain_pct = (highest / p1 - 1.0) * 100
        avg_gain_pct  = (current_price / p1 - 1.0) * 100
        _EPS = 1e-8

        logger.debug(
            "Risk v3 [%s]: price=%.0f high=%.0f peakGain=%.1f%% "
            "avgGain=%.1f%% ctp=%s ladderGen=%d ladderLen=%d",
            position.get("strategy", "?"), current_price, highest,
            peak_gain_pct, avg_gain_pct, ctp_active,
            ctp_ladder_gen, len(ctp_ladder),
        )

        # ════════════════════════════════════════════════════════════
        # 1. Fatal Stop
        # ════════════════════════════════════════════════════════════
        fatal_level = p1 - self.atr_fatal_mult * atr
        if current_price <= fatal_level:
            logger.warning("FATAL: price %.0f <= %.0f", current_price, fatal_level)
            return RiskDecision(
                RiskAction.SELL_ALL,
                amount_krw=int(amount_krw),
                reason="Fatal Stop (%.0f <= %.0f)" % (current_price, fatal_level),
            )

        # ═══════════════════════════════════════════════════════════════════
        # 2. 〽️ CTP (Cascade Take-Profit)
        #    고점에서 사다리 4개 설치 → 순차 익절 + 새 고점 시 리셋
        # ═══════════════════════════════════════════════════════════════════

        if peak_gain_pct >= self.ctp_activate_pct - _EPS and highest > ctp_ladder_peak + _EPS:
            # 새 고점 → 사다리 설치 (또는 재설치)
            if not ctp_active:
                position["ctp_active"] = True
                ctp_active = True
                logger.info("〽️ CTP ARMED [%s]: peak +%.1f%% >= +%.0f%%",
                            position.get("strategy", "?"), peak_gain_pct,
                            self.ctp_activate_pct)

            # 사다리 비중: 첫 번째 vs 이후
            if ctp_ladder_gen == 0:
                ratios = [0.10, 0.20, 0.50, 1.00]
                ladder_dd_pcts = [1.0, 3.0, 5.0, 7.0]
                logger.info("〽️ CTP 1st ladder [%s]: peak=%.0f (10→20→50→100%%)",
                            position.get("strategy", "?"), highest)
            else:
                ratios = [0.10, 0.10, 0.20, 0.60]
                ladder_dd_pcts = [1.0, 2.5, 4.0, 6.0]
                logger.info("〽️ CTP gen%d ladder [%s]: peak=%.0f (10→10→20→60%%)",
                            ctp_ladder_gen + 1, position.get("strategy", "?"), highest)

            # Build ladder: [(trigger_price, sell_ratio, step_number), ...]
            new_ladder = []
            for i, (dd, ratio) in enumerate(zip(ladder_dd_pcts, ratios)):
                trigger = highest * (1.0 - dd / 100.0)
                new_ladder.append((trigger, ratio, i + 1))

            position["ctp_ladder"] = new_ladder
            position["ctp_ladder_peak"] = highest
            position["ctp_ladder_gen"] = ctp_ladder_gen + 1
            ctp_ladder = new_ladder
            ctp_ladder_gen = position["ctp_ladder_gen"]

        # Ladder pop: ascending → highest trigger first
        while ctp_ladder:
            trigger, ratio, step = ctp_ladder[0]
            if current_price > trigger + _EPS:
                break  # Not reached yet

            ctp_ladder.pop(0)  # Consume

            # Final step: check avg profit floor
            is_final_step = (step == 4) or (ratio >= 0.50 and len(ctp_ladder) == 0)
            min_profit_price = p1 * (1.0 + self.ctp_min_profit_pct / 100.0)

            if is_final_step and current_price < min_profit_price - _EPS:
                logger.info(
                    "〽️ CTP #%d BLOCKED [%s]: final step but "
                    "price %.0f < avg+%.0f%%(%.0f) — holding",
                    step, position.get("strategy", "?"), current_price,
                    self.ctp_min_profit_pct, min_profit_price,
                )
                ctp_ladder.insert(0, (trigger, ratio, step))
                break

            sell_krw = int(amount_krw * ratio)
            sell_krw = max(5_000, min(sell_krw, int(amount_krw) - 5_000))

            # Small position → sell all
            if sell_krw < 5_000 or sell_krw >= int(amount_krw) - 5_000:
                logger.info("〽️ CTP #%d→SELL_ALL [%s]: small (₩%dkr)",
                            step, position.get("strategy", "?"), int(amount_krw // 1000))
                return RiskDecision(
                    RiskAction.SELL_ALL,
                    amount_krw=int(amount_krw),
                    reason="CTP #%d/4: final step (₩%dkr, avg+%.1f%%)" % (
                        step, int(amount_krw // 1000), avg_gain_pct),
                )

            logger.info(
                "〽️ CTP #%d [%s]: %.0f ≤ trigger %.0f → SELL %dK/%dK (%.0f%%) [avg+%.1f%%]",
                step, position.get("strategy", "?"), current_price, trigger,
                sell_krw // 1000, int(amount_krw) // 1000, ratio * 100, avg_gain_pct,
            )
            return RiskDecision(
                RiskAction.SELL_PARTIAL,
                amount_krw=sell_krw,
                reason="CTP #%d/4: %.0f≤%.0f (%.0f%%) [avg+%.1f%%]" % (
                    step, current_price, trigger, ratio * 100, avg_gain_pct),
            )

        # �════════════════════════════════════════════════════════════════
        # 3. ATR Stage 1 Trail (CTP off only)
        # �════════════════════════════════════════════════════════════════
        if not ctp_active and exit_stage < 1:
            threshold1 = highest - self.atr_stage1_mult * atr
            if current_price <= threshold1:
                sell_krw = max(5_000, int(amount_krw * self.atr_sell_frac))
                sell_krw = min(sell_krw, int(amount_krw) - 5_000)
                if sell_krw >= 5_000:
                    logger.info("Stage1 Exit: %.0f <= %.0f", current_price, threshold1)
                    return RiskDecision(
                        RiskAction.SELL_PARTIAL, amount_krw=sell_krw,
                        reason="Stage 1 Exit (h-1ATR) %.0f<=%.0f" % (
                            current_price, threshold1),
                    )

        # �════════════════════════════════════════════════════════════════
        # 4. ATR Stage 2 Trail (CTP off only)
        # �════════════════════════════════════════════════════════════════
        if not ctp_active and exit_stage < 2:
            threshold2 = highest - self.atr_stage2_mult * atr
            if current_price <= threshold2:
                sell_krw = max(5_000, int(amount_krw * self.atr_sell_frac))
                sell_krw = min(sell_krw, int(amount_krw) - 5_000)
                if sell_krw >= 5_000:
                    logger.info("Stage2 Exit: %.0f <= %.0f", current_price, threshold2)
                    return RiskDecision(
                        RiskAction.SELL_PARTIAL, amount_krw=sell_krw,
                        reason="Stage 2 Exit (h-2ATR) %.0f<=%.0f" % (
                            current_price, threshold2),
                    )

        # �════════════════════════════════════════════════════════════════
        # 5. Waterfall (average down)
        #    �avg -5% → blocked
        # �════════════════════════════════════════════════════════════════
        if avg_gain_pct <= self.water_floor_pct - _EPS:
            logger.debug("Waterfall BLOCKED: avgGain=%.1f%% <= floor %.0f%%",
                         avg_gain_pct, self.water_floor_pct)
        elif stage <= self.water_max_stages:
            watter_atr_lvl = p1 - self.water_atr_mult * atr
            water_pct_lvl = p1 * (1 - self.water_avg_pct / 100)
            water_trigger = max(watter_atr_lvl, water_pct_lvl)
            if current_price <= water_trigger:
                mult_idx = max(0, stage - 1)
                mult = self.water_mults[mult_idx]
                add_krw = int(unit_size_krw * mult)
                logger.info("Waterfall Stage %d: add KRW %d (mult=%.1f)",
                            stage + 1, add_krw, mult)
                return RiskDecision(
                    RiskAction.BUY_DOWN, amount_krw=add_krw,
                    reason="Waterfall Stage %d (avg-%.1fATR or -%.0f%%)" % (
                        stage + 1, self.water_atr_mult, self.water_avg_pct),
                )

        # �════════════════════════════════════════════════════════════════
        # 6. Fire (pyramid up)
        #    �CTP active → blocked
        # �════════════════════════════════════════════════════════════════
        if ctp_active:
            logger.debug("Fire BLOCKED: ctp_active=True")
        elif fire_count < self.fire_max_count:
            fire_threshold = last_buy + self.fire_atr_mult * atr
            if current_price >= fire_threshold:
                logger.info("Fire %d: add KRW %d", fire_count + 1, unit_size_krw)
                return RiskDecision(
                    RiskAction.BUY_UP, amount_krw=unit_size_krw,
                    reason="Fire +1U (%d/%d)" % (fire_count + 1, self.fire_max_count),
                )

        return RiskDecision(RiskAction.HOLD, reason="No action")


# ───────────────────────────────────────────────────────────────────────

def make_position(
    entry_price: float, atr: float, amount_krw: int,
    strategy: str, current_price: Optional[float] = None,
) -> dict:
    """Create a position dict for BaekdooCoin CTP v3."""
    price = current_price if current_price else entry_price
    return {
        "entry_price":      entry_price,
        "last_buy_price":   price,
        "atr":              atr,
        "stage":            1,
        "fire_count":       0,
        "highest_price":    price,
        "ctp_active":       False,
        "ctp_ladder":       [],        # v3: [(trigger, ratio, step), ...]
        "ctp_ladder_peak":  0.0,       # v3: ladder install peak
        "ctp_ladder_gen":   0,         # v3: ladder generation (1=first)
        "exit_stage":       0,
        "amount_krw":       amount_krw,
        "strategy":         strategy,
    }


# ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rm = RiskManager(ctp_activate_pct=3.0, ctp_min_profit_pct=2.0)

    # Test 1: CTP ladder arms at +3% peak
    pos = make_position(100000, 5000, 100000, "turtle")
    pos["highest_price"] = 103500  # +3.5% → CTP arms, 1st ladder
    dec = rm.evaluate(pos, 103000, 100000)
    assert pos["ctp_active"], "CTP should be active"
    assert len(pos["ctp_ladder"]) == 4, f"Expected 4 steps, got {len(pos['ctp_ladder'])}"
    assert pos["ctp_ladder_gen"] == 1
    ratios = [r for _, r, _ in pos["ctp_ladder"]]
    assert ratios == [0.10, 0.20, 0.50, 1.00], f"Expected 10/20/50/100, got {ratios}"
    print(f"TEST1✅ CTP armed: {len(pos['ctp_ladder'])} steps, ratios={ratios}")

    # Test 2: Step 1 fires at peak-1%
    pos2 = make_position(100000, 5000, 200000, "turtle")
    pos2["highest_price"] = 103000  # +3% → CTP arms
    dec2 = rm.evaluate(pos2, 101500, 150000)  # below trigger 103000*0.99=101970
    assert dec2.action == RiskAction.SELL_PARTIAL, f"Expected SELL_PARTIAL, got {dec2.action}"
    assert "CTP #1" in dec2.reason
    print(f"TEST2✅ CTP step 1: {dec2.reason}, remain={len(pos2['ctp_ladder'])}")

    # Test 3: New peak resets ladder (2nd gen → conservactive ratios)
    pos3 = make_position(100000, 5000, 200000, "turtle")
    pos3["highest_price"] = 103000
    rm.evaluate(pos3, 102500, 150000)  # CTP arms, gen=1
    pos3["highest_price"] = 110000  # new peak, +10%
    rm.evaluate(pos3, 109000, 150000)  # CTP resets, gen=2 (conservactive)
    assert pos3["ctp_ladder_gen"] == 2
    ratios2 = [r for _, r, _ in pos3["ctp_ladder"]]
    assert ratios2 == [0.10, 0.10, 0.20, 0.60], f"Expected 10/10/20/60, got {ratios2}"
    print(f"TEST3✅ CTP 2nd gen reset: ratios={ratios2}")

    # Test 4: Fire blocked when CTP active
    pos4 = make_position(100000, 5000, 100000, "turtle")
    pos4["ctp_active"] = True
    pos4["highest_price"] = 106000
    dec = rm.evaluate(pos4, 103000, 100000)
    assert dec.action != RiskAction.BUY_UP, f"Fire should be blocked, got {dec.action}"
    print(f"TEST4✅ fire blocked: {dec.action}")

    # Test 5: Waterfall blocked at -5%
    pos5 = make_position(100000, 5000, 100000, "turtle")
    dec = rm.evaluate(pos5, 94500, 100000)
    assert dec.action != RiskAction.BUY_DOWN, f"Waterfall should be blocked, got {dec.action}"
    print(f"TEST5✅ waterfall blocked at -5%: {dec.action}")

    # Test 6: Full cascade — 3-step ladder drop
    pos6 = make_position(100000, 5000, 300000, "turtle")
    pos6["highest_price"] = 105000  # +5% → CTP arms
    rm.evaluate(pos6, 104000, 150000)  # install ladder, no trigger yet
    decisions = []
    for drop_price in [103500, 101000, 98000]:
        dec = rm.evaluate(pos6, drop_price, 150000)
        decisions.append(dec.action.value)
    print(f"TEST6 cascade: {decisions}")
    assert "SELL_PARTIAL" in decisions, f"No CTP step triggered: {decisions}"

    print("\n✅✅ ALL CTP v3 TESTS PASSED")