# -*- coding: utf-8 -*-
"""CTP (Cascade Take-Profit) 사다리 단위 테스트 — pandas 불필요.

원본 `strategies/risk.py` 의 CTP 를 MTF 리스크매니저로 이식한 것에 대한 검증.
이 파일은 pandas 없이 실행된다:  pytest tests/test_ctp_ladder.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:                                               # pandas 없는 환경에서도 실행되도록
    from strategies.risk_mtf import MTFRiskManager      # noqa: E402
except ImportError:                                # pragma: no cover
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "risk_mtf", Path(__file__).resolve().parent.parent / "strategies" / "risk_mtf.py")
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)                 # type: ignore[union-attr]
    MTFRiskManager = _mod.MTFRiskManager           # type: ignore[assignment,misc]

EQ = 10_000_000


@pytest.fixture()
def rm():
    m = MTFRiskManager(equity_krw=EQ)
    m.add_position("KRW-TEST", "1D", entry_price=100_000.0, atr=1_000.0,
                   amount_krw=1_000_000, risk_pct=0.01)
    return m


def test_ctp_not_armed_below_three_percent(rm):
    rm.evaluate_ctp("KRW-TEST", 102_000.0)          # +2%
    assert rm.get_position("KRW-TEST").ctp_active is False


def test_ctp_arms_at_three_percent_and_installs_four_rungs(rm):
    rm.evaluate_ctp("KRW-TEST", 103_500.0)          # +3.5%
    pos = rm.get_position("KRW-TEST")
    assert pos.ctp_active is True
    assert len(pos.ctp_ladder) == 4
    assert pos.ctp_ladder_gen == 1


def test_ctp_step1_sells_ten_percent_on_fill(rm):
    rm.evaluate_ctp("KRW-TEST", 103_500.0)
    trigger = rm.get_position("KRW-TEST").ctp_ladder[0][0]
    act = rm.evaluate_ctp("KRW-TEST", trigger - 10)
    assert act["action"] == "SELL_PARTIAL" and act["ratio"] == pytest.approx(0.10)
    # 체결 전에는 rung 이 소비되지 않는다
    assert len(rm.get_position("KRW-TEST").ctp_ladder) == 4
    remaining = rm.apply_partial_sell("KRW-TEST", trigger - 10, act["ratio"])
    assert len(rm.get_position("KRW-TEST").ctp_ladder) == 3
    assert remaining < 1_000_000


def test_ctp_failed_order_restores_rung(rm):
    """주문 실패 시 rung 이 사라지면 사다리가 조용히 붕괴한다."""
    rm.evaluate_ctp("KRW-TEST", 103_500.0)
    trigger = rm.get_position("KRW-TEST").ctp_ladder[0][0]
    rm.evaluate_ctp("KRW-TEST", trigger - 10)       # 체결 대기 상태
    assert rm.restore_pending("KRW-TEST") is True
    assert len(rm.get_position("KRW-TEST").ctp_ladder) == 4


def test_ctp_new_peak_reinstalls_conservative_ladder(rm):
    rm.evaluate_ctp("KRW-TEST", 103_500.0)
    rm.evaluate_ctp("KRW-TEST", 106_000.0)          # 새 고점
    pos = rm.get_position("KRW-TEST")
    assert pos.ctp_ladder_gen == 2
    assert sorted(r for _, r, _ in pos.ctp_ladder) == [0.10, 0.10, 0.20, 0.60]


def test_ctp_high_weight_rungs_are_reachable(rm):
    """ratio>=0.5 rung 의 트리거가 평단+2% 방어선보다 아래면 영구 교착이 된다."""
    rm.evaluate_ctp("KRW-TEST", 104_000.0)          # +4% → 3·4단이 방어선보다 아래
    min_price = 100_000.0 * 1.02
    pos = rm.get_position("KRW-TEST")
    for trig, ratio, _ in pos.ctp_ladder:
        if ratio >= 0.5:
            assert trig >= min_price - 1e-6


def test_ctp_dumps_remainder_at_profit_floor(rm):
    """고점 +4% 수익 후 방어선까지 밀리면 잔량이 모두 소진돼야 한다."""
    rm.evaluate_ctp("KRW-TEST", 104_000.0)
    sold = 0.0
    for px in (103_000.0, 102_500.0, 102_100.0, 102_000.0, 101_999.0):
        for _ in range(4):
            act = rm.evaluate_ctp("KRW-TEST", px)
            if not act or act["action"] != "SELL_PARTIAL":
                break
            sold += act["ratio"]
            rm.apply_partial_sell("KRW-TEST", px, act["ratio"])
            if rm.get_position("KRW-TEST") is None:
                break
    assert sold > 0.5, f"소진 비율 {sold:.2f} — 3·4단이 도달 불가"


def test_ctp_average_price_updates_on_pyramid(rm):
    """불타기로 평단이 올라가면 CTP 발동선도 따라 올라가야 한다."""
    before = rm.get_position("KRW-TEST").effective_avg_price
    rm.record_add("KRW-TEST", 110_000.0, 500_000)
    after = rm.get_position("KRW-TEST").effective_avg_price
    assert after > before
    assert rm.get_position("KRW-TEST").amount_krw == 1_500_000


def test_ctp_full_exit_removes_position(rm):
    rm.evaluate_ctp("KRW-TEST", 110_000.0)          # +10% 고점 → 사다리 설치
    pos = rm.get_position("KRW-TEST")
    pos.ctp_ladder = [[108_000.0, 1.0, 4]]          # 마지막 단만 남김
    act = rm.evaluate_ctp("KRW-TEST", 107_000.0)
    assert act["action"] == "SELL_PARTIAL"
    rm.apply_partial_sell("KRW-TEST", 107_000.0, 1.0)
    assert rm.get_position("KRW-TEST") is None


def test_ctp_realized_pnl_is_recorded(rm):
    rm.evaluate_ctp("KRW-TEST", 103_500.0)
    trigger = rm.get_position("KRW-TEST").ctp_ladder[0][0]
    act = rm.evaluate_ctp("KRW-TEST", trigger - 10)
    rm.apply_partial_sell("KRW-TEST", trigger - 10, act["ratio"])
    assert rm.get_position("KRW-TEST").realized_krw > 0
