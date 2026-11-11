# -*- coding: utf-8 -*-
"""라이브 모니터 단위 테스트 — 주문 실행 경로/PID 락/텔레그램 명령."""
from __future__ import annotations

import os

import pytest

import datastore
import live_monitor
import paths
from config import Config
from engine_mtf import MTFEngine
from tests.conftest import make_candles

TICKERS = ["KRW-BTC"]


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
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    paths.ensure_dirs()
    return rt


@pytest.fixture()
def db(tmp_db):
    rng = __import__("numpy").random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, 90)
    close = __import__("numpy").concatenate([base, [base.max() * 1.25]])
    high = __import__("numpy").concatenate([base * 1.005, [base.max() * 1.26]])
    low = __import__("numpy").concatenate([base * 0.995, [base.max() * 1.24]])
    c = make_candles(close, high=high, low=low, ticker="KRW-BTC")
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                    in zip(c.ts, c.open, c.high, c.low, c.close, c.volume, c.volume_krw)])
    return tmp_db


class FakeClient:
    def __init__(self, fail=False, equity=10_000_000, price=131.0):
        self.fail = fail
        self.equity = equity
        self.price = price
        self.buys, self.sells = [], []

    def get_total_equity_krw(self):
        return self.equity

    def get_krw_balance(self):
        return self.equity

    def get_all_balances(self):
        return {}

    def get_current_price(self, ticker):
        return self.price

    def market_buy(self, ticker, amount, client_order_id=None):
        if self.fail:
            raise RuntimeError("주문 거부")
        self.buys.append((ticker, amount))
        return {"uuid": "b", "state": "done"}

    def market_sell(self, ticker, qty, client_order_id=None):
        if self.fail:
            raise RuntimeError("주문 거부")
        self.sells.append((ticker, qty))
        return {"uuid": "s", "state": "done"}


def _cfg(db) -> Config:
    return Config(db_path=db, tickers=TICKERS, initial_equity_krw=10_000_000)


def _monitor(db, trade=False, client=None, use_ws=False) -> live_monitor.LiveMonitor:
    mon = live_monitor.LiveMonitor(_cfg(db), trade=trade, use_ws=use_ws)
    if client is not None:
        mon.client = client
        mon.engine.client = client
    return mon


# ---------------- PID 락 ----------------
def test_pid_lock_acquire_and_release(runtime):
    lock = live_monitor.PidLock(paths.RUNTIME_DIR / "live.pid")
    assert lock.acquire() is True
    assert (paths.RUNTIME_DIR / "live.pid").read_text().strip() == str(os.getpid())
    lock.release()
    assert not (paths.RUNTIME_DIR / "live.pid").exists()


def test_pid_lock_blocks_second_instance(runtime):
    path = paths.RUNTIME_DIR / "live.pid"
    first = live_monitor.PidLock(path)
    assert first.acquire() is True
    second = live_monitor.PidLock(path)
    assert second.acquire() is False


def test_pid_lock_reclaims_dead_pid(runtime):
    path = paths.RUNTIME_DIR / "live.pid"
    path.write_text("999999")                     # 존재하지 않는 PID
    assert live_monitor.PidLock(path).acquire() is True
    assert path.read_text().strip() == str(os.getpid())


def test_pid_alive_helper():
    assert live_monitor._pid_alive(os.getpid()) is True
    assert live_monitor._pid_alive(0) is False


# ---------------- 생성/자본 ----------------
def test_monitor_uses_exchange_total_equity(db, runtime, monkeypatch):
    """실거래 모드에서는 계좌 총 평가액을 자본으로 쓴다(현금만 쓰면 과거 오탐 버그)."""
    import upbit_client
    monkeypatch.setattr(upbit_client.UpbitClient, "get_total_equity_krw", lambda self: 7_654_321.0)
    monkeypatch.setattr(upbit_client.UpbitClient, "get_all_balances", lambda self: {})
    cfg = Config(db_path=db, tickers=TICKERS, initial_equity_krw=1_000_000,
                 upbit_access_key="k", upbit_secret_key="s")
    mon = live_monitor.LiveMonitor(cfg, trade=False, use_ws=False)
    assert mon.engine.equity_krw == pytest.approx(7_654_321)


def test_monitor_falls_back_to_config_equity(db, runtime):
    mon = _monitor(db)
    assert mon.engine.equity_krw == pytest.approx(10_000_000)


# ---------------- 주문 실행 ----------------
def test_paper_mode_does_not_call_exchange(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=False, client=client)
    res = mon.run_once()
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert buys, "돌파 데이터에서는 진입 주문이 나와야 한다"
    assert client.buys == []                       # 모의 모드: 실제 주문 없음
    assert mon.engine.risk.get("KRW-BTC") is not None


def test_trade_mode_places_order_and_updates_position(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=True, client=client)
    res = mon.run_once()
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert len(client.buys) == 1 and client.buys[0][0] == "KRW-BTC"
    pos = mon.engine.risk.get("KRW-BTC")
    assert pos is not None and pos.extras["qty"] > 0
    assert buys[0]["kind"] == "BUY"


def test_order_failure_rolls_back_position(db, runtime):
    client = FakeClient(fail=True)
    mon = _monitor(db, trade=True, client=client)
    res = mon.run_once()
    assert [o for o in res["orders"] if o["kind"] == "BUY"]
    assert mon.engine.risk.get("KRW-BTC") is None        # 실패 → 포지션 롤백
    assert mon.engine._daily_trades_count == 0


def test_sell_order_uses_position_qty(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=True, client=client)
    mon.run_once()
    pos = mon.engine.risk.get("KRW-BTC")
    order = {"kind": "SELL_PARTIAL", "ticker": "KRW-BTC", "ratio": 0.5, "price": 150.0,
             "reason": "test", "pre_state": mon.engine._pos_state(pos)}
    assert mon._execute(order) is True
    assert client.sells and client.sells[0][1] == pytest.approx(pos.extras["qty"] * 0.5)


def test_sell_without_position_is_not_sent(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=True, client=client)
    assert mon._execute({"kind": "SELL_ALL", "ticker": "KRW-ETH", "ratio": 1.0, "price": 1.0}) is False
    assert client.sells == []


# ---------------- 텔레그램 핸들러 ----------------
def test_telegram_stop_creates_kill_switch(db, runtime):
    mon = _monitor(db)
    msg = mon._tg_stop()
    assert paths.emergency_active() is True
    assert "비상정지" in msg


def test_telegram_resume_clears_kill_switch(db, runtime):
    mon = _monitor(db)
    mon._tg_stop()
    mon._tg_resume()
    assert paths.emergency_active() is False


def test_telegram_status_contains_equity(db, runtime):
    mon = _monitor(db)
    text = mon._tg_status()
    assert "자본" in text and "모드" in text


def test_telegram_regime_and_data(db, runtime):
    mon = _monitor(db)
    assert "레짐" in mon._tg_regime()
    assert "DB" in mon._tg_data() or "통계" in mon._tg_data()
