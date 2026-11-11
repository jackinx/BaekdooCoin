# -*- coding: utf-8 -*-
"""리스크 매니저 / CTP 사다리 단위 테스트 — 실전 매매에서 돈이 새는 지점."""
from __future__ import annotations

import pytest

from strategies.risk_mtf import (ATR_DOWN, CAP_1D_SLEEVE, CAP_PORTFOLIO, CTP_ACTIVATE_PCT,
                                 MTFRiskManager, SleevePosition)

EQ = 10_000_000


def _pos(**kw) -> SleevePosition:
    base = dict(ticker="KRW-TEST", sleeve="1D", strategy="turtle_1d", entry_price=100_000.0,
                atr=1_000.0, amount_krw=1_000_000.0, unit_krw=500_000.0, units=1,
                last_buy_price=100_000.0, highest_price=100_000.0, risk_pct=0.01, max_pyramid=2)
    base.update(kw)
    return SleevePosition(**base)


def _rm_with(pos: SleevePosition, qty: float = 10.0) -> MTFRiskManager:
    rm = MTFRiskManager(equity_krw=EQ)
    rm.register(pos, qty=qty)
    return rm


# ---------------- 진입 승인 / 캡 ----------------
def test_approve_entry_rejects_duplicate_ticker():
    rm = _rm_with(_pos())
    amount, reason = rm.approve_entry("KRW-TEST", "1D", 100_000, 100_000, 1_000)
    assert amount == 0.0 and "이미" in reason


def test_approve_entry_rejects_when_sleeve_full():
    rm = MTFRiskManager(equity_krw=EQ)
    for i in range(5):
        rm.register(_pos(ticker=f"KRW-{i}", risk_pct=0.001), qty=1.0)
    amount, reason = rm.approve_entry("KRW-NEW", "1D", 100_000, 100_000, 1_000)
    assert amount == 0.0 and "종목수" in reason


def test_approve_entry_shrinks_at_sleeve_cap():
    """슬리브 캡(6%)을 넘으면 비례 축소되어 승인 금액이 요청보다 작아야 한다."""
    rm = MTFRiskManager(equity_krw=EQ)
    rm.register(_pos(ticker="KRW-A", risk_pct=0.055), qty=1.0)
    amount, reason = rm.approve_entry("KRW-B", "1D", 3_000_000, 100_000, 3_000)
    assert 0 < amount < 3_000_000
    assert "축소" in reason
    assert rm.sleeve_risk("1D") <= CAP_1D_SLEEVE + 1e-9


def test_approve_entry_rejects_below_minimum_order():
    rm = MTFRiskManager(equity_krw=EQ)
    amount, _ = rm.approve_entry("KRW-A", "1D", 4_000, 100_000, 1_000)
    assert amount == 0.0


def test_portfolio_cap_blocks_new_sleeve():
    rm = MTFRiskManager(equity_krw=EQ)
    rm.register(_pos(ticker="KRW-A", risk_pct=CAP_PORTFOLIO), qty=1.0)   # 총캡 소진
    amount, reason = rm.approve_entry("KRW-B", "5M", 100_000, 100_000, 1_000)
    assert amount == 0.0
    assert "총캡" in reason or "슬리브" in reason


def test_total_risk_never_exceeds_cap_after_approvals():
    rm = MTFRiskManager(equity_krw=EQ)
    approved = 0
    for i in range(10):
        a, _ = rm.approve_entry(f"KRW-{i}", "1D", 2_000_000, 100_000, 3_000)
        if a > 0:
            rm.register(_pos(ticker=f"KRW-{i}", risk_pct=(2 * 3_000 / 100_000) * (a / EQ)), qty=a / 100_000)
            approved += 1
    assert rm.total_risk() <= CAP_PORTFOLIO + 1e-6


# ---------------- 우선순위 ----------------
def test_fatal_stop_sells_all():
    rm = _rm_with(_pos())
    act = rm.evaluate("KRW-TEST", 96_000)          # entry - 3×ATR = 97,000
    assert act.kind == "SELL_ALL" and "Fatal" in act.reason


def test_no_position_returns_hold():
    assert MTFRiskManager().evaluate("KRW-NONE", 100).kind == "HOLD"


def test_ctp_arms_at_three_percent_peak():
    rm = _rm_with(_pos())
    assert rm.evaluate("KRW-TEST", 102_000).kind != "SELL_PARTIAL"
    assert rm.get("KRW-TEST").ctp_active is False       # +2% : 아직 미발동
    act = rm.evaluate("KRW-TEST", 103_500)              # +3.5% : 발동
    assert act.kind == "HOLD" and "CTP" in act.reason
    p = rm.get("KRW-TEST")
    assert p.ctp_active and len(p.ctp_ladder) == 4


def test_pyramid_precedes_ctp_arm_below_3pct():
    """+0.5ATR 상승이면 CTP 발동 전에 불타기가 먼저 나간다 (원본 우선순위 그대로).

    ⚠️ 부작용: 평단이 계속 올라가 CTP 발동선(+3%)이 멀어진다 — 감사 항목으로 관리.
    """
    rm = _rm_with(_pos())
    assert rm.evaluate("KRW-TEST", 102_000).kind == "BUY_UP"


def test_ctp_ladder_triggers_descend_from_peak():
    rm = _rm_with(_pos())
    rm.evaluate("KRW-TEST", 103_500)
    triggers = [t for t, _, _ in rm.get("KRW-TEST").ctp_ladder]
    assert triggers == sorted(triggers, reverse=True)
    peak = rm.get("KRW-TEST").ctp_ladder_peak
    assert triggers[0] == pytest.approx(peak * 0.99)


def test_ctp_step1_sells_ten_percent():
    rm = _rm_with(_pos())
    rm.evaluate("KRW-TEST", 103_500)
    trigger = rm.get("KRW-TEST").ctp_ladder[0][0]
    act = rm.evaluate("KRW-TEST", trigger - 10)
    assert act.kind == "SELL_PARTIAL" and act.ratio == pytest.approx(0.10)
    assert len(rm.get("KRW-TEST").ctp_ladder) == 4           # 제안 단계에서는 소비하지 않는다
    rm.on_sell("KRW-TEST", trigger - 10, act.ratio)          # 체결 확정
    assert len(rm.get("KRW-TEST").ctp_ladder) == 3


def test_ctp_new_peak_reinstalls_ladder():
    rm = _rm_with(_pos())
    rm.evaluate("KRW-TEST", 103_500)
    before = rm.get("KRW-TEST").ctp_ladder_gen
    act = rm.evaluate("KRW-TEST", 106_000)
    p = rm.get("KRW-TEST")
    assert p.ctp_ladder_gen == before + 1 and len(p.ctp_ladder) == 4
    assert act.kind == "HOLD"
    assert sorted(r for _, r, _ in p.ctp_ladder) == [0.10, 0.10, 0.20, 0.60]   # N차는 보수적


def test_ctp_partial_sell_reduces_qty_and_realizes_pnl():
    rm = _rm_with(_pos(), qty=10.0)
    rm.evaluate("KRW-TEST", 103_500)
    trigger = rm.get("KRW-TEST").ctp_ladder[0][0]
    act = rm.evaluate("KRW-TEST", trigger - 10)
    rm.on_sell("KRW-TEST", trigger - 10, act.ratio)
    p = rm.get("KRW-TEST")
    assert p.extras["qty"] == pytest.approx(9.0)
    assert p.realized_krw > 0


def test_ctp_full_exit_removes_position():
    rm = _rm_with(_pos(), qty=10.0)
    rm.get("KRW-TEST").highest_price = 110_000        # 이미 +10% 고점
    rm.evaluate("KRW-TEST", 110_000)                  # 사다리 설치
    p = rm.get("KRW-TEST")
    p.ctp_ladder = [[108_000.0, 1.0, 4]]              # 마지막 단만 남김
    act = rm.evaluate("KRW-TEST", 107_000)
    assert act.kind == "SELL_PARTIAL"
    rm.on_sell("KRW-TEST", 107_000, 1.0)
    assert rm.get("KRW-TEST") is None


# ---------------- 물타기 / 불타기 ----------------
def test_pyramid_up_fires_half_atr_above_last_buy():
    rm = _rm_with(_pos())
    act = rm.evaluate("KRW-TEST", 100_500 + 1)        # last_buy + 0.5×ATR
    assert act.kind == "BUY_UP" and act.amount_krw == pytest.approx(500_000)


def test_pyramid_blocked_while_ctp_active():
    rm = _rm_with(_pos())
    rm.evaluate("KRW-TEST", 103_500)                  # CTP 발동
    act = rm.evaluate("KRW-TEST", 103_600)            # 사다리 위 → pop 없음, 불타기 금지
    assert act.kind == "HOLD"


def test_pyramid_respects_max_units():
    rm = _rm_with(_pos(max_pyramid=2))
    p = rm.get("KRW-TEST")
    p.fire_count = 2
    assert rm.evaluate("KRW-TEST", 101_000).kind != "BUY_UP"


def test_water_down_triggers_on_atr_step():
    rm = _rm_with(_pos(), qty=10.0)
    trig = 100_000 - (1 * ATR_DOWN + ATR_DOWN) * 1_000     # = 98,600
    act = rm.evaluate("KRW-TEST", trig - 1)
    assert act.kind == "BUY_DOWN"


def test_water_down_blocked_below_five_percent():
    """평단 -5% 이하에서는 물타기 금지 (추세 이탈 방어)."""
    rm = _rm_with(_pos(atr=2_000.0), qty=10.0)             # 손절선 = 94,000
    act = rm.evaluate("KRW-TEST", 94_500)
    assert act.kind == "HOLD" and "물타기 차단" in act.reason


def test_on_buy_updates_average_and_units():
    rm = _rm_with(_pos(), qty=10.0)
    rm.on_buy("KRW-TEST", 101_000, 505_000, kind="BUY_UP")
    p = rm.get("KRW-TEST")
    assert p.units == 2 and p.fire_count == 1
    assert p.avg_price == pytest.approx(p.amount_krw / p.extras["qty"])
    assert p.last_buy_price == 101_000


# ---------------- 상태 저장/복구 ----------------
def test_json_roundtrip_preserves_ctp_state():
    rm = _rm_with(_pos(), qty=10.0)
    rm.evaluate("KRW-TEST", 103_500)
    blob = rm.to_json()
    rm2 = MTFRiskManager(equity_krw=EQ)
    rm2.load_json(blob)
    p = rm2.get("KRW-TEST")
    assert p.ctp_active is True and len(p.ctp_ladder) == 4
    assert p.extras["qty"] == pytest.approx(10.0)


def test_from_dict_ignores_unknown_fields():
    """구버전 파일(bone_armed 등)을 읽어도 죽지 않아야 한다."""
    old = {"ticker": "KRW-OLD", "sleeve": "1D", "strategy": "turtle_1d",
           "entry_price": 100.0, "atr": 1.0, "amount_krw": 10_000.0,
           "bone_armed": True, "carve_levels": [1, 2, 3]}
    rm = MTFRiskManager()
    rm.load_json({"KRW-OLD": old})
    assert rm.get("KRW-OLD") is not None
