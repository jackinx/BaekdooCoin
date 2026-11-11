# -*- coding: utf-8 -*-
"""엔진 단위 테스트 — 실전 주문 경로/안전장치/상태 영속화."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import datastore
import engine_mtf
import paths
from engine_mtf import MTFEngine
from strategies.risk_mtf import SleevePosition
from tests.conftest import make_candles

TICKERS = ["KRW-BTC", "KRW-ETH"]


# ---------------- 픽스처 ----------------
@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    monkeypatch.setattr(paths, "RUNTIME_DIR", rt)
    monkeypatch.setattr(paths, "POSITIONS_FILE", rt / "open_positions.json")
    monkeypatch.setattr(paths, "POSITIONS_PAPER_FILE", rt / "open_positions_paper.json")
    monkeypatch.setattr(paths, "STATE_FILE", rt / "engine_state.json")
    monkeypatch.setattr(paths, "EMERGENCY_STOP_FILE", rt / "EMERGENCY_STOP")
    monkeypatch.setattr(paths, "PAUSE_FILE", rt / "PAUSED")
    monkeypatch.setattr(paths, "TRADES_FILE", rt / "trades.jsonl")
    monkeypatch.setattr(paths, "STATE_FILE", rt / "engine_state.json")
    monkeypatch.setattr(paths, "EMERGENCY_STOP_FILE", rt / "EMERGENCY_STOP")
    paths.ensure_dirs()
    return rt


def _breakout_series(n_base=90, jump=1.25):
    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, n_base)
    close = np.concatenate([base, [base.max() * jump]])
    high = np.concatenate([base * 1.005, [base.max() * jump * 1.01]])
    low = np.concatenate([base * 0.995, [base.max() * jump * 0.99]])
    return make_candles(close, high=high, low=low)


def _flat_series(n=100):
    return make_candles(np.full(n, 100.0))


@pytest.fixture()
def db_with_breakout(tmp_db):
    """BTC 는 55일 신고가 돌파, ETH 는 횡보."""
    for t in ("KRW-BTC",):
        c = _breakout_series()
        datastore.upsert(tmp_db, "1d", [(t, ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                        in zip(c.ts, c.open, c.high, c.low, c.close, c.volume, c.volume_krw)])
    c = _flat_series()
    datastore.upsert(tmp_db, "1d", [("KRW-ETH", ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                    in zip(c.ts, c.open, c.high, c.low, c.close, c.volume, c.volume_krw)])
    return tmp_db


def _engine(db, runtime, equity=10_000_000, **kw) -> MTFEngine:
    return MTFEngine(TICKERS, db, equity_krw=equity, **kw)


class FakeClient:
    def __init__(self, balances=None, equity=None):
        self.balances = balances or {}
        self.equity = equity
        self.buys, self.sells = [], []

    def get_all_balances(self):
        return self.balances

    def get_total_equity_krw(self):
        return self.equity if self.equity is not None else 0.0

    def get_krw_balance(self):
        return 1_000_000.0

    def market_buy(self, ticker, amount, client_order_id=None):
        self.buys.append((ticker, amount))
        return {"uuid": "b1", "state": "done", "executed_volume": amount / 100_000,
                "avg_price": 100_000}

    def market_sell(self, ticker, qty, client_order_id=None):
        self.sells.append((ticker, qty))
        return {"uuid": "s1", "state": "done"}


# ---------------- 진입 ----------------
def test_breakout_emits_buy_and_registers_position(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    res = eng.run_cycle()
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert len(buys) == 1 and buys[0]["ticker"] == "KRW-BTC"
    assert buys[0]["amount_krw"] >= 5_000
    pos = eng.risk.get("KRW-BTC")
    assert pos is not None and pos.sleeve == "1D"
    assert pos.extras["qty"] > 0
    assert res["positions"][0]["ticker"] == "KRW-BTC"


def test_no_duplicate_entry_for_same_ticker(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    second = eng.run_cycle()
    assert [o for o in second["orders"] if o["kind"] == "BUY"] == []


def test_flat_market_produces_no_orders(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    res = eng.run_cycle(live_prices={"KRW-ETH": 100.0})
    # ETH 는 횡보(채널 돌파 없음) → 진입 없음. BTC 는 완료 일봉이 돌파 상태라 진입 가능(F09).
    assert [o for o in res["orders"] if o["ticker"] == "KRW-ETH"] == []


def test_entry_respects_order_minimum(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime, equity=50_000)     # 1% 리스크 = 500원
    res = eng.run_cycle()
    for o in res["orders"]:
        if o["kind"] in ("BUY", "BUY_UP", "BUY_DOWN"):
            assert o["amount_krw"] >= 5_000


def test_live_price_drives_entry(db_with_breakout, runtime):
    """DB 종가는 돌파 전이지만 실시간가가 채널을 넘으면 진입해야 한다."""
    eng = _engine(db_with_breakout, runtime)
    eng.as_of = None
    res = eng.run_cycle(live_prices={"KRW-ETH": 130.0})
    assert [o for o in res["orders"] if o["ticker"] == "KRW-ETH"] == []   # ETH 는 채널도 낮음


# ---------------- 안전장치 ----------------
def test_kill_switch_blocks_all_orders(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    paths.emergency_stop()
    res = eng.run_cycle()
    assert res["orders"] == []
    assert "Kill-switch" in res["emergency_stop"]


def test_daily_loss_limit_blocks(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime, equity=10_000_000)
    eng._daily_starting_equity = 10_000_000
    eng.equity_krw = 9_400_000                        # -6%
    res = eng.run_cycle()
    assert res["orders"] == []
    assert "Daily loss" in res["emergency_stop"]


def test_daily_trade_limit_blocks(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime, max_daily_trades=0)
    res = eng.run_cycle()
    assert res["orders"] == []
    assert "Daily trade limit" in res["emergency_stop"]


def test_cycle_order_limit_defers_buys_instead_of_stopping(db_with_breakout, runtime, monkeypatch):
    """한도 초과는 정지가 아니라 매수 이월 — 청산은 절대 폐기하지 않는다(감사 F04)."""
    eng = _engine(db_with_breakout, runtime)
    monkeypatch.setattr(eng, "max_orders_per_cycle", 0)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []
    assert any("이월" in s for s in res["summary"])
    assert res["emergency_stop"] is None


def test_resume_clears_kill_switch(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    paths.emergency_stop()
    eng.resume()
    assert paths.emergency_active() is False
    assert eng.run_cycle()["emergency_stop"] is None


# ---------------- 체결 반영 / 롤백 ----------------
def test_on_order_filled_buy_up_adds_units(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    pos = eng.risk.get("KRW-BTC")
    qty_before, units_before = pos.extras["qty"], pos.units
    order = {"kind": "BUY_UP", "ticker": "KRW-BTC", "amount_krw": 200_000, "price": 130.0}
    eng.on_order_filled(order, fill_price=130.0)
    pos = eng.risk.get("KRW-BTC")
    assert pos.units == units_before + 1 and pos.extras["qty"] > qty_before


def test_rollback_buy_removes_position_and_restores_count(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    res = eng.run_cycle()
    order = [o for o in res["orders"] if o["kind"] == "BUY"][0]
    eng.rollback_order(order, "주문 실패")
    assert eng.risk.get("KRW-BTC") is None
    assert eng._daily_trades_count == 0


def test_rollback_buy_up_restores_qty(db_with_breakout, runtime):
    """부분 주문 실패 시 낙관적으로 반영한 증액이 남아 있으면 포지션이 부풀려진다."""
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    pos = eng.risk.get("KRW-BTC")
    qty0, units0, amount0 = pos.extras["qty"], pos.units, pos.amount_krw
    order = {"kind": "BUY_UP", "ticker": "KRW-BTC", "amount_krw": 200_000, "price": 130.0,
             "pre_state": eng._pos_state(pos)}
    eng.risk.on_buy("KRW-BTC", 130.0, 200_000, kind="BUY_UP")     # 엔진이 낙관 반영
    eng.rollback_order(order, "주문 실패")
    pos = eng.risk.get("KRW-BTC")
    assert pos.extras["qty"] == pytest.approx(qty0)
    assert pos.units == units0
    assert pos.amount_krw == pytest.approx(amount0)


def test_rollback_without_snapshot_inverts_arithmetic(db_with_breakout, runtime):
    """스냅샷이 없는 경로(구버전 주문 dict)에서도 역산으로 복원돼야 한다."""
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    pos = eng.risk.get("KRW-BTC")
    qty0, units0 = pos.extras["qty"], pos.units
    order = {"kind": "BUY_DOWN", "ticker": "KRW-BTC", "amount_krw": 100_000, "price": 100.0}
    eng.risk.on_buy("KRW-BTC", 100.0, 100_000, kind="BUY_DOWN")
    eng.rollback_order(order, "주문 실패")
    pos = eng.risk.get("KRW-BTC")
    assert pos.extras["qty"] == pytest.approx(qty0)
    assert pos.units == units0


def test_rollback_sell_all_restores_position(db_with_breakout, runtime):
    """매도가 실패했는데 포지션을 지워버리면 이후 관리가 끊긴다(유령 포지션)."""
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    pos = eng.risk.get("KRW-BTC")
    qty0 = pos.extras["qty"]
    order = {"kind": "SELL_ALL", "ticker": "KRW-BTC", "ratio": 1.0, "price": 50.0,
             "pre_state": eng._pos_state(pos)}
    eng.on_order_filled(order, fill_price=50.0)          # 낙관 반영 → 포지션 제거
    assert eng.risk.get("KRW-BTC") is None
    eng.rollback_order(order, "매도 실패")
    restored = eng.risk.get("KRW-BTC")
    assert restored is not None and restored.extras["qty"] == pytest.approx(qty0)


# ---------------- 영속화 / 복구 ----------------
def test_persist_and_reload_positions(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    eng.persist()
    blob = json.loads(paths.POSITIONS_FILE.read_text(encoding="utf-8"))
    assert "KRW-BTC" in blob
    eng2 = _engine(db_with_breakout, runtime)
    assert eng2.risk.get("KRW-BTC") is not None
    assert eng2.risk.get("KRW-BTC").sleeve == "1D"


def test_state_file_restores_daily_counters(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    eng._daily_trades_count = 4
    eng.persist()
    eng2 = _engine(db_with_breakout, runtime)
    assert eng2._daily_trades_count == 4


def test_corrupt_state_file_does_not_crash(db_with_breakout, runtime):
    paths.POSITIONS_FILE.write_text("{not json", encoding="utf-8")
    eng = _engine(db_with_breakout, runtime)
    assert eng.risk.positions == {}


def test_sync_from_exchange_registers_existing_holdings(db_with_breakout, runtime):
    client = FakeClient(balances={"KRW-XRP": {"qty": 100.0, "avg_price": 2_000.0,
                                             "value_krw": 200_000.0, "price": 2_000.0}})
    eng = _engine(db_with_breakout, runtime, client=client)
    assert eng.sync_from_exchange() == 1
    pos = eng.risk.get("KRW-XRP")
    assert pos is not None and pos.extras["qty"] == pytest.approx(100.0)


def test_sync_skips_dust_and_keeps_known_positions(db_with_breakout, runtime):
    client = FakeClient(balances={
        "KRW-DUST": {"qty": 0.0001, "avg_price": 10.0, "value_krw": 50.0, "price": 10.0},
    })
    eng = _engine(db_with_breakout, runtime, client=client)
    assert eng.sync_from_exchange() == 0


# ---------------- as_of (리플레이) ----------------
def test_as_of_truncates_future_candles(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    full = eng._load("1d", "KRW-BTC")
    eng.as_of = full.ts[-2][:10]                    # 마지막 봉 이전
    cut = eng._load("1d", "KRW-BTC")
    assert len(cut) == len(full) - 1


def test_as_of_before_first_candle_returns_none(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.as_of = "2000-01-01"
    assert eng._load("1d", "KRW-BTC") is None


# ---------------- 스냅샷 / 상태 문구 ----------------
def test_snapshot_reports_pnl_and_ctp(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    snap = eng._snapshot_positions({"KRW-BTC": 200.0})
    assert snap[0]["pnl_pct"] > 0
    assert snap[0]["ctp"] in ("ON", "OFF")


def test_status_lines_include_equity_and_emergency(db_with_breakout, runtime):
    eng = _engine(db_with_breakout, runtime)
    eng.run_cycle()
    paths.emergency_stop()
    text = "\n".join(eng.status_lines())
    assert "자본" in text and "EMERGENCY" in text
