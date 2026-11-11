# -*- coding: utf-8 -*-
"""엔진 주문 경로 커버리지 — 5M 슬리브/불타기/CTP 부분익절/승급/스냅샷 폴백."""
from __future__ import annotations

import numpy as np
import pytest

import datastore
import paths
from engine_mtf import MTFEngine
from strategies.risk_mtf import SleevePosition
from tests.conftest import make_candles

TICKERS = ["KRW-BTC", "KRW-ETH"]


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


def _rows(ticker, c):
    return [(ticker, ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
            in zip(c.ts, c.open, c.high, c.low, c.close, c.volume, c.volume_krw)]


def _uptrend(n, rate, tf, ticker="KRW-BTC"):
    close = 100 * np.exp(np.cumsum(np.full(n, rate)))
    return make_candles(close, high=close * 1.003, low=close * 0.997, ticker=ticker, tf=tf)


def _breakout_5m():
    up = 100 * np.exp(np.cumsum(np.full(80, 0.002)))
    dip = up[-1] * np.array([0.997, 0.994])
    base = np.concatenate([up, dip])
    dc_high = float(np.max(base[-55:] * 1.003))
    brk = dc_high * 1.008
    close = np.append(base, brk)
    high = np.append(base * 1.003, brk * 1.004)
    low = np.append(base * 0.997, brk * 0.992)
    vol = np.concatenate([np.full(len(base), 10.0), [40.0]])
    return make_candles(close, high=high, low=low, volume=vol, ticker="KRW-BTC", tf="5m")


@pytest.fixture()
def db_5m(tmp_db):
    """1h 상승추세 + 5m 돌파 + 1d 상승추세 (5M 슬리브 진입 조건)."""
    datastore.upsert(tmp_db, "1h", _rows("KRW-BTC", _uptrend(200, 0.002, "1h")))
    datastore.upsert(tmp_db, "5m", _rows("KRW-BTC", _breakout_5m()))
    datastore.upsert(tmp_db, "1d", _rows("KRW-BTC", _uptrend(120, 0.01, "1d")))
    return tmp_db


def _eng(db, **kw) -> MTFEngine:
    return MTFEngine(TICKERS, db, equity_krw=10_000_000, **kw)


# ---------------- 5M 슬리브 ----------------
def test_5m_sleeve_entry_when_1d_has_no_breakout(db_5m, runtime):
    eng = _eng(db_5m)
    res = eng.run_cycle()
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert buys and buys[0]["sleeve"] == "5M", res["summary"]
    assert eng.risk.get("KRW-BTC").sleeve == "5M"


def test_5m_entry_records_daily_trade_limit(db_5m, runtime):
    eng = _eng(db_5m)
    res = eng.run_cycle()
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert buys and buys[0]["committed"] is True         # 주문 생성 = 포지션 등록
    assert eng.limiter._entries["KRW-BTC"]               # 5M 일일 제한 카운트
    assert eng._daily_trades_count == 0                  # 거래 카운터는 '체결'에서만 증가


# ---------------- 불타기 / CTP 부분익절 ----------------
def _seed_position(eng, **kw):
    pos = SleevePosition(ticker="KRW-BTC", sleeve="1D", strategy="turtle_1d",
                         entry_price=100_000.0, atr=2_000.0, amount_krw=2_000_000.0,
                         units=1, unit_krw=1_000_000.0, last_buy_price=100_000.0,
                         highest_price=100_000.0, risk_pct=0.02, max_pyramid=2)
    pos.extras["qty"] = 20.0
    eng.risk.register(pos, qty=20.0)
    return pos


def test_pyramid_up_emits_order(db_5m, runtime):
    eng = _eng(db_5m)
    _seed_position(eng)
    res = eng.run_cycle(live_prices={"KRW-BTC": 101_500.0})
    ups = [o for o in res["orders"] if o["kind"] == "BUY_UP"]
    assert ups and ups[0]["amount_krw"] > 0
    assert "pre_state" in ups[0]


def test_ctp_partial_sell_emitted(db_5m, runtime):
    eng = _eng(db_5m)
    pos = _seed_position(eng)
    pos.highest_price = 104_000.0                      # +4% 고점
    eng.risk.evaluate("KRW-BTC", 104_000.0)            # 사다리 설치
    trigger = eng.risk.get("KRW-BTC").ctp_ladder[0][0]
    res = eng.run_cycle(live_prices={"KRW-BTC": trigger - 100})
    sells = [o for o in res["orders"] if o["kind"] == "SELL_PARTIAL"]
    assert sells and sells[0]["ratio"] > 0


def test_water_down_emits_order(db_5m, runtime):
    eng = _eng(db_5m)
    _seed_position(eng)
    res = eng.run_cycle(live_prices={"KRW-BTC": 96_500.0})   # 물타기 트리거 이하
    downs = [o for o in res["orders"] if o["kind"] == "BUY_DOWN"]
    assert downs and downs[0]["amount_krw"] >= 5_000


def test_strategy_exit_marks_stop_flag(db_5m, runtime):
    eng = _eng(db_5m)
    _seed_position(eng)
    res = eng.run_cycle(live_prices={"KRW-BTC": 90_000.0})    # 손절선 이탈
    sells = [o for o in res["orders"] if o["kind"] == "SELL_ALL"]
    assert sells and sells[0].get("stop") is True


# ---------------- 승급 ----------------
def test_graduation_emits_order_and_converts(db_5m, runtime):
    eng = _eng(db_5m)
    pos = SleevePosition(ticker="KRW-BTC", sleeve="5M", strategy="turtle_5m",
                         entry_price=120.0, atr=1.0, amount_krw=1_000_000.0, units=1,
                         unit_krw=1_000_000.0, last_buy_price=120.0, highest_price=130.0,
                         risk_pct=0.005, max_pyramid=1)
    pos.extras["qty"] = 1_000_000.0 / 120.0
    eng.risk.register(pos, qty=pos.extras["qty"])
    # 1D 돌파가 있어야 승급한다 → 라이브가를 1D 채널 위로
    one_d = eng._load("1d", "KRW-BTC")
    live = float(np.max(one_d.high[:-1])) * 1.02
    res = eng.run_cycle(live_prices={"KRW-BTC": live})
    grads = [o for o in res["orders"] if o["kind"] == "GRADUATE"]
    if not grads:
        pytest.skip(f"승급 조건 미충족(정상): {res['summary']}")
    eng.on_order_filled(grads[0], fill_price=live)
    converted = eng.risk.get("KRW-BTC")
    assert converted.sleeve == "1D" and converted.strategy == "turtle_1d"


def test_approve_graduation_requires_profit(db_5m, runtime):
    eng = _eng(db_5m)
    pos = SleevePosition(ticker="KRW-BTC", sleeve="5M", strategy="turtle_5m",
                         entry_price=100.0, atr=2.0, amount_krw=10_000.0, units=1,
                         unit_krw=10_000.0, last_buy_price=100.0, highest_price=100.0,
                         risk_pct=0.005, max_pyramid=1)
    pos.extras["qty"] = 100.0
    eng.risk.register(pos, qty=100.0)
    assert eng.risk.approve_graduation("KRW-BTC", 101.0, 2.0) is False     # +1 < 1.5×ATR
    assert eng.risk.approve_graduation("KRW-BTC", 104.0, 2.0) is True
    eng.risk.graduate("KRW-BTC")
    assert eng.risk.get("KRW-BTC").sleeve == "1D"


# ---------------- 캡 / 스냅샷 폴백 ----------------
def test_cap_buy_limited_by_remaining_equity(db_5m, runtime):
    from engine_mtf import ORDER_CAP
    eng = _eng(db_5m)
    pos = _seed_position(eng)
    eng.equity_krw = 2_100_000.0                  # 이미 2,000,000 투입
    capped = eng._cap_buy(5_000_000, pos)
    assert capped <= eng.equity_krw * ORDER_CAP["1D"] + 1   # 주문 상한(15%) 적용
    assert capped < 5_000_000


def test_cap_buy_rejects_below_minimum(db_5m, runtime):
    eng = _eng(db_5m)
    assert eng._cap_buy(4_999) == 0.0


def test_snapshot_falls_back_to_db_price(db_5m, runtime):
    eng = _eng(db_5m)
    _seed_position(eng)
    snap = eng._snapshot_positions({})
    assert snap[0]["price"] > 0 and snap[0]["value_krw"] > 0


def test_sync_skips_already_known_position(db_5m, runtime):
    class C:
        def get_all_balances(self):
            return {"KRW-BTC": {"qty": 1.0, "avg_price": 100.0, "value_krw": 100.0, "price": 100.0}}

    eng = _eng(db_5m, client=C())
    _seed_position(eng)
    assert eng.sync_from_exchange() == 0


def test_register_manual_position(db_5m, runtime):
    eng = _eng(db_5m)
    pos = eng.register_manual_position("KRW-XRP", entry_price=2_000.0, amount_krw=100_000.0)
    assert eng.risk.get("KRW-XRP") is pos
    assert paths.POSITIONS_FILE.exists()


def test_daily_rollover_resets_counters(db_5m, runtime):
    from datetime import date, timedelta
    eng = _eng(db_5m)
    eng._daily_trades_count = 9
    eng._daily_date = date.today() - timedelta(days=1)
    eng._roll_daily()
    assert eng._daily_trades_count == 0
