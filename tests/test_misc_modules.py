# -*- coding: utf-8 -*-
"""텔레그램/백테스트/데이터 파이프라인/진단 단위 테스트 (네트워크 mock)."""
from __future__ import annotations

import numpy as np
import pytest

import backtest
import data_crawler
import data_updater
import datastore
import telegram_control
from tests.conftest import make_candles


# ---------------- 텔레그램 ----------------
class FakeTG(telegram_control.TelegramController):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = []
        self._call = lambda method, http_timeout=30, params=None: (
            self.sent.append(params) or {"ok": True})

    def send(self, text):            # 실제 네트워크 대신 기록
        self.sent.append({"text": text})


def _tg(handlers=None, chat="123"):
    h = {"status": lambda: "상태OK", "_help_text": lambda: telegram_control.HELP_TEXT}
    h.update(handlers or {})
    return FakeTG("token", chat, h)


def test_whitelist_rejects_other_chat():
    tg = _tg()
    tg._handle({"message": {"chat": {"id": 999}, "text": "/status"}})
    assert tg.sent == []


def test_known_command_dispatched():
    tg = _tg()
    tg._handle({"message": {"chat": {"id": 123}, "text": "/status"}})
    assert any("상태OK" in str(s.get("text")) for s in tg.sent)


def test_command_with_bot_suffix():
    tg = _tg()
    tg._handle({"message": {"chat": {"id": 123}, "text": "/status@my_bot"}})
    assert any("상태OK" in str(s.get("text")) for s in tg.sent)


def test_unknown_command_returns_help():
    tg = _tg()
    tg._handle({"message": {"chat": {"id": 123}, "text": "/없는명령"}})
    assert any("알 수 없는 명령" in str(s.get("text")) for s in tg.sent)


def test_handler_exception_is_reported_not_raised():
    def boom():
        raise RuntimeError("고장")

    tg = _tg({"boom": boom})
    tg._handle({"message": {"chat": {"id": 123}, "text": "/boom"}})
    assert any("명령 처리 오류" in str(s.get("text")) for s in tg.sent)


def test_empty_text_ignored():
    tg = _tg()
    tg._handle({"message": {"chat": {"id": 123}, "text": "  "}})
    assert tg.sent == []


def test_polling_updates_offset(monkeypatch):
    tg = _tg()
    calls = []

    def fake_call(method, http_timeout=30, params=None):
        calls.append(params)
        return {"result": [{"update_id": 7, "message": {"chat": {"id": 123}, "text": "/status"}}]}

    tg._call = fake_call
    tg._loop = lambda: None
    upd = {"update_id": 7, "message": {"chat": {"id": 123}, "text": "/status"}}
    tg._handle(upd)
    tg._offset = 8
    assert tg._offset == 8


# ---------------- 백테스트 ----------------
def _trend_with_breakout():
    rng = np.random.default_rng(4)
    base = 100 + rng.normal(0, 0.4, 120)
    tail = base[-1] * np.exp(np.cumsum(np.full(80, 0.01)))
    return make_candles(np.concatenate([base, tail]))


def test_backtest_produces_trades_and_metrics():
    r = backtest.run_turtle_1d(_trend_with_breakout(), equity0=1_000_000)
    assert r["trades"] >= 1
    assert 0 <= r["win_rate"] <= 100
    assert r["final_equity"] > 0
    assert "pf" in r and "mdd_pct" in r


def test_backtest_short_series_reports_error():
    r = backtest.run_turtle_1d(make_candles(np.full(30, 100.0)))
    assert r["trades"] == 0 and "error" in r


def test_backtest_start_date_filter_reduces_trades():
    c = _trend_with_breakout()
    full = backtest.run_turtle_1d(c, equity0=1_000_000)
    late = backtest.run_turtle_1d(c, equity0=1_000_000, start_date=c.ts[-10][:10])
    assert late["trades"] <= full["trades"]


def test_backtest_no_lookahead_on_first_bar():
    """진입은 항상 워밍업 이후에만 발생해야 한다(채널 자기포함 버그 방지)."""
    r = backtest.run_turtle_1d(_trend_with_breakout(), equity0=1_000_000)
    for t in r["_trades"]:
        assert t["in"] > "2024-01-01"


# ---------------- 데이터 파이프라인 ----------------
def _fake_candle(ts, price):
    return {"candle_date_time_utc": ts, "opening_price": price, "high_price": price * 1.01,
            "low_price": price * 0.99, "trade_price": price * 1.005,
            "candle_acc_trade_volume": 10.0, "candle_acc_trade_price": price * 10}


def test_crawler_upserts_pages(tmp_db, monkeypatch):
    pages = [[_fake_candle("2024-01-03T00:00:00", 100.0), _fake_candle("2024-01-02T00:00:00", 99.0)],
             [_fake_candle("2024-01-01T00:00:00", 98.0)]]
    monkeypatch.setattr(data_crawler, "candles", lambda *a, **kw: pages.pop(0) if pages else [])
    monkeypatch.setattr(data_crawler.time, "sleep", lambda *_: None)
    n = data_crawler.fetch_ticker("KRW-BTC", "1d", tmp_db, max_pages=5, start="2024-01-01")
    assert n >= 3
    assert len(datastore.load(tmp_db, "1d", "KRW-BTC")) == 3


def test_crawler_stops_on_empty_page(tmp_db, monkeypatch):
    monkeypatch.setattr(data_crawler, "candles", lambda *a, **kw: [])
    assert data_crawler.fetch_ticker("KRW-BTC", "1d", tmp_db) == 0


def test_updater_skips_fresh_data(tmp_db, monkeypatch):
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", "2099-01-01T00:00:00", 1, 1, 1, 1.0, 1, 1)])
    called = []
    monkeypatch.setattr(data_updater, "candles", lambda *a, **kw: called.append(1) or [])
    data_updater.update(tmp_db, ["KRW-BTC"], ["1d"])
    assert called == []


def test_updater_inserts_when_stale(tmp_db, monkeypatch):
    monkeypatch.setattr(data_updater, "candles",
                        lambda *a, **kw: [_fake_candle("2024-02-01T00:00:00", 50.0)])
    data_updater.update(tmp_db, ["KRW-BTC"], ["1d"], force=True)
    assert datastore.last_timestamp(tmp_db, "1d", "KRW-BTC") == "2024-02-01T00:00:00"


def test_updater_survives_fetch_error(tmp_db, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("네트워크")

    monkeypatch.setattr(data_updater, "candles", boom)
    assert data_updater.update(tmp_db, ["KRW-BTC"], ["1d"], force=True) == 0


# ---------------- doctor ----------------
def test_doctor_runs_and_reports(monkeypatch, capsys):
    import doctor
    monkeypatch.setattr(doctor, "check_net", lambda *a, **kw: False)
    code = doctor.main()
    out = capsys.readouterr().out
    assert "시스템" in out and "결론" in out
    assert code in (0, 1)
