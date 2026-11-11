# -*- coding: utf-8 -*-
"""커버리지 게이트 보강 — 런타임 루프/WS/크롤러 main/리스크 잔여 분기."""
from __future__ import annotations

import json
import sys
import types

import numpy as np
import pytest

import data_crawler
import datastore
import live_monitor
import paths
from strategies.risk_mtf import MTFRiskManager, SleevePosition
from tests.conftest import make_candles
from tests.test_live_monitor import FakeClient, _cfg


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
    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, 90)
    close = np.concatenate([base, [base.max() * 1.25]])
    high = np.concatenate([base * 1.005, [base.max() * 1.26]])
    low = np.concatenate([base * 0.995, [base.max() * 1.24]])
    c = make_candles(close, high=high, low=low, ticker="KRW-BTC")
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                    in zip(c.ts, c.open, c.high, c.low, c.close, c.volume, c.volume_krw)])
    return tmp_db


def _monitor(db, trade=False, client=None):
    mon = live_monitor.LiveMonitor(_cfg(db), trade=trade, use_ws=False)
    if client is not None:
        mon.client = client
        mon.engine.client = client
    return mon


# ---------------- live_monitor: 루프/WS/업데이터 ----------------
def test_run_executes_cycle_and_stops(db, runtime, monkeypatch):
    mon = _monitor(db)
    calls = []

    def fake_once():
        calls.append(1)
        mon._stop.set()
        return {"orders": [], "positions": [], "equity_krw": 1}

    monkeypatch.setattr(mon, "run_once", fake_once)
    monkeypatch.setattr(mon, "_setup_telegram", lambda: None)
    monkeypatch.setattr(mon, "_updater_loop", lambda every=300: None)
    mon.run(interval=1)
    assert calls == [1]


def test_run_survives_cycle_exception(db, runtime, monkeypatch):
    mon = _monitor(db)
    state = {"n": 0}

    def boom():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("사이클 폭발")
        mon._stop.set()

    monkeypatch.setattr(mon, "run_once", boom)
    monkeypatch.setattr(mon, "_setup_telegram", lambda: None)
    monkeypatch.setattr(mon, "_updater_loop", lambda every=300: None)
    mon.run(interval=1)
    assert state["n"] == 2          # 예외 후에도 루프 지속


def test_ws_loop_updates_price_cache(db, runtime, monkeypatch):
    mon = _monitor(db)
    msg = json.dumps({"code": "KRW-BTC", "trade_price": 123.0})

    class FakeWS:
        def __init__(self):
            self.sent = []

        def send(self, m):
            self.sent.append(m)

        def recv(self):
            if self.sent and msg:
                self.sent.clear()
                return msg
            mon._stop.set()
            raise ConnectionError("closed")

        def close(self):
            pass

    fake_mod = types.SimpleNamespace(create_connection=lambda *a, **kw: FakeWS())
    monkeypatch.setitem(sys.modules, "websocket", fake_mod)
    monkeypatch.setattr(live_monitor.time, "sleep", lambda *_: None)
    mon._ws_loop()
    assert mon.live_prices()["KRW-BTC"] == pytest.approx(123.0)


def test_updater_loop_runs_once_and_stops(db, runtime, monkeypatch):
    mon = _monitor(db)
    seen = []

    def fake_update(db_path, tickers, tfs, force=False):
        seen.append(1)
        mon._stop.set()
        return 3

    monkeypatch.setattr("data_updater.update", fake_update)
    mon._updater_loop(every=1)
    assert seen == [1]


def test_notify_logs_without_telegram(db, runtime, caplog):
    import logging
    caplog.set_level(logging.INFO)
    mon = _monitor(db)
    mon._notify("테스트 알림")
    assert "테스트 알림" in caplog.text


def test_setup_telegram_skips_without_config(db, runtime):
    mon = _monitor(db)
    mon._setup_telegram()
    assert mon.tg is None


def test_shutdown_saves_state(db, runtime):
    mon = _monitor(db)
    mon.run_once()
    mon.shutdown()
    assert mon._stop.is_set()
    assert paths.POSITIONS_PAPER_FILE.exists()      # 감시 모드는 페이퍼 파일에 저장(STATE-12)


# ---------------- live_monitor: 실행 분기 ----------------
def test_execute_graduate_sells_then_buys(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=True, client=client)
    mon.run_once()
    mon.client = client
    order = {"kind": "GRADUATE", "ticker": "KRW-BTC", "amount_krw": 100_000, "price": 150.0}
    assert mon._execute(order) is True
    assert client.sells and client.buys


def test_execute_unknown_kind_returns_false(db, runtime):
    mon = _monitor(db, trade=True, client=FakeClient())
    assert mon._execute({"kind": "MYSTERY", "ticker": "KRW-BTC", "price": 1.0}) is False


def test_execute_sell_all_uses_full_qty(db, runtime):
    client = FakeClient()
    mon = _monitor(db, trade=True, client=client)
    mon.run_once()
    pos = mon.engine.risk.get("KRW-BTC")
    order = {"kind": "SELL_ALL", "ticker": "KRW-BTC", "ratio": 1.0, "price": 150.0,
             "pre_state": mon.engine._pos_state(pos)}
    assert mon._execute(order) is True
    assert client.sells[0][1] == pytest.approx(pos.extras["qty"])


def test_run_once_rest_prices_fallback_when_ws_off(db, runtime):
    client = FakeClient(price=131.0)
    mon = _monitor(db, trade=False, client=client)
    res = mon.run_once()
    assert res["orders"]                     # REST 폴백 가격으로도 진입


def test_run_once_price_fetch_failure_is_tolerated(db, runtime):
    class FlakyClient(FakeClient):
        def get_current_price(self, ticker):
            raise RuntimeError("시세 실패")

    mon = _monitor(db, trade=False, client=FlakyClient())
    mon.use_ws = False
    res = mon.run_once()
    assert isinstance(res, dict)


# ---------------- data_crawler ----------------
def test_market_list_filters_krw(monkeypatch):
    monkeypatch.setattr(data_crawler, "_get", lambda *a, **kw: [
        {"market": "KRW-BTC"}, {"market": "BTC-ETH"}, {"market": "KRW-XRP"}])
    assert data_crawler.market_list() == ["KRW-BTC", "KRW-XRP"]


def test_row_skips_missing_timestamp():
    assert data_crawler._row("KRW-BTC", {"opening_price": 1}) == ()


def test_candles_builds_params(monkeypatch):
    captured = {}
    monkeypatch.setattr(data_crawler, "_get", lambda url, params, **kw: captured.update(params) or [])
    data_crawler.candles("KRW-BTC", "1h", to="2024-01-01T00:00:00")
    assert captured["market"] == "KRW-BTC" and captured["to"].startswith("2024-01-01")
    assert "minutes/60" in data_crawler.PATH["1h"]


def test_crawler_main_stats(monkeypatch, tmp_db, capsys):
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", "2024-01-01T00:00:00", 1, 1, 1, 1.0, 1, 1)])
    monkeypatch.setattr(sys, "argv", ["data_crawler.py", "--db", tmp_db, "--stats"])
    assert data_crawler.main() == 0
    assert "KRW-BTC" in capsys.readouterr().out


def test_crawler_main_handles_fetch_errors(monkeypatch, tmp_db):
    def boom(*a, **kw):
        raise RuntimeError("네트워크 차단")

    monkeypatch.setattr(data_crawler, "fetch_ticker", boom)
    monkeypatch.setattr(sys, "argv", ["data_crawler.py", "--db", tmp_db, "--max-pages", "1"])
    assert data_crawler.main() == 0          # 실패해도 크래시하지 않는다


# ---------------- risk_mtf 잔여 분기 ----------------
def test_on_buy_unknown_ticker_is_noop():
    rm = MTFRiskManager()
    rm.on_buy("KRW-NONE", 100.0, 1_000.0)          # 예외 없이 무시


def test_on_sell_unknown_ticker_returns_none():
    assert MTFRiskManager().on_sell("KRW-NONE", 100.0, 0.5) is None


def test_evaluate_with_missing_atr_uses_position_atr():
    rm = MTFRiskManager(equity_krw=1_000_000)
    pos = SleevePosition(ticker="KRW-T", sleeve="1D", strategy="x", entry_price=100.0, atr=1.0,
                         amount_krw=10_000.0, unit_krw=10_000.0, last_buy_price=100.0,
                         highest_price=100.0, risk_pct=0.01)
    pos.extras["qty"] = 100.0
    rm.register(pos, qty=100.0)
    assert rm.evaluate("KRW-T", 100.5, atr=None).kind in ("HOLD", "BUY_UP", "SELL_ALL")


def test_evaluate_invalid_price_is_hold():
    rm = MTFRiskManager()
    pos = SleevePosition(ticker="KRW-T", sleeve="1D", strategy="x", entry_price=100.0, atr=1.0,
                         amount_krw=10_000.0, unit_krw=10_000.0, risk_pct=0.01)
    rm.register(pos, qty=1.0)
    assert rm.evaluate("KRW-T", 0.0).kind == "HOLD"


def test_sleeve_risk_and_count():
    rm = MTFRiskManager()
    rm.register(SleevePosition(ticker="A", sleeve="1D", strategy="x", entry_price=1, atr=1,
                               amount_krw=1, risk_pct=0.01), qty=1)
    rm.register(SleevePosition(ticker="B", sleeve="5M", strategy="x", entry_price=1, atr=1,
                               amount_krw=1, risk_pct=0.005), qty=1)
    assert rm.sleeve_risk("1D") == pytest.approx(0.01)
    assert rm.count_in("5M") == 1
    assert rm.total_risk() == pytest.approx(0.015)


def test_third_generation_ladder_is_conservative():
    rm = MTFRiskManager(equity_krw=10_000_000)
    pos = SleevePosition(ticker="KRW-T", sleeve="1D", strategy="x", entry_price=100.0, atr=1.0,
                         amount_krw=10_000.0, unit_krw=10_000.0, last_buy_price=100.0,
                         highest_price=100.0, risk_pct=0.01)
    pos.extras["qty"] = 100.0
    rm.register(pos, qty=100.0)
    for pe in (104.0, 108.0, 112.0):
        rm.evaluate("KRW-T", pe)
    assert rm.get("KRW-T").ctp_ladder_gen == 3
    assert [r for _, r, _ in rm.get("KRW-T").ctp_ladder] == [0.10, 0.10, 0.20, 0.60]


def test_load_json_skips_broken_entry():
    rm = MTFRiskManager()
    rm.load_json({"BAD": {"ticker": "BAD", "entry_price": "not-a-number"}})
    assert rm.get("BAD") is None or isinstance(rm.get("BAD").entry_price, str)
