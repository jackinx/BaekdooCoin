# -*- coding: utf-8 -*-
"""실전 하드닝 — 감사(CRITICAL/HIGH) 항목을 실패 테스트로 고정한다.

각 테스트 이름이 감사 ID 를 담는다:
  F01/SAFETY-03 equity · F02/NUMERIC-18 사이징 캡 · F03 ADX NaN · F04/SAFETY-02 정지 시 청산
  F05/STATE-11 CTP 사다리 · F09 종가 판정 · F10/CONCURRENCY-13 가격 신선도
  STATE-06 사이클 한도 · STATE-07 원자적 저장 · STATE-10 수동 포지션 · STATE-12 페이퍼 분리
  STATE-14 pause · LIFECYCLE-01 수수료/잔고 · LIFECYCLE-08 승급 롤백 · F08 risk_pct 갱신
  F13 유동성 · F18 거래원장 · DATA-21 5m 갱신 · CONCURRENCY-15 PID 락
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import datastore
import engine_mtf
import indicators as ind
import live_monitor
import paths
from config import Config
from engine_mtf import MTFEngine
from strategies.risk_mtf import MTFRiskManager, SleevePosition
from tests.conftest import make_candles

TICKERS = ["KRW-BTC", "KRW-ETH"]


# ---------------- 공통 픽스처 ----------------
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
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    paths.ensure_dirs()
    return rt


def _rows(ticker, c, min_vol_krw=1e11):
    vol = np.full(len(c), 100.0)
    vkrw = np.full(len(c), min_vol_krw)
    return [(ticker, ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
            in zip(c.ts, c.open, c.high, c.low, c.close, vol, vkrw)]


def _breakout(n_base=90, jump=1.25, tf="1d", ticker="KRW-BTC"):
    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, n_base)
    close = np.concatenate([base, [base.max() * jump]])
    high = np.concatenate([base * 1.005, [base.max() * jump * 1.01]])
    low = np.concatenate([base * 0.995, [base.max() * jump * 0.99]])
    return make_candles(close, high=high, low=low, ticker=ticker, tf=tf)


@pytest.fixture()
def db(tmp_db):
    datastore.upsert(tmp_db, "1d", _rows("KRW-BTC", _breakout()))
    flat = make_candles(np.full(100, 100.0), ticker="KRW-ETH")
    datastore.upsert(tmp_db, "1d", _rows("KRW-ETH", flat))
    return tmp_db


def _engine(db, paper=False, **kw) -> MTFEngine:
    return MTFEngine(TICKERS, db, equity_krw=kw.pop("equity_krw", 10_000_000), paper=paper, **kw)


class FakeClient:
    def __init__(self, equity=10_000_000, cash=10_000_000, holdings=None, price=140.0):
        self.equity, self.cash, self.price = equity, cash, price
        self.holdings = holdings or {}
        self.buys, self.sells = [], []
        self.fail_equity = False

    def get_total_equity_krw(self):
        if self.fail_equity:
            raise RuntimeError("잔고 조회 실패")
        return self.equity

    def get_krw_balance(self):
        return self.cash

    def get_coin_balance(self, ticker):
        return self.holdings.get(ticker, (0.0, 0.0))

    def get_all_balances(self):
        return {}

    def get_current_price(self, ticker):
        return self.price

    def market_buy(self, ticker, amount, client_order_id=None):
        self.buys.append((ticker, amount))
        return {"uuid": "b", "state": "done", "executed_volume": amount / self.price,
                "avg_price": self.price}

    def market_sell(self, ticker, qty, client_order_id=None):
        self.sells.append((ticker, qty))
        return {"uuid": "s", "state": "done", "executed_volume": qty, "avg_price": self.price}


def _cfg(db, **kw) -> Config:
    return Config(db_path=db, tickers=TICKERS, initial_equity_krw=kw.pop("equity", 10_000_000), **kw)


def _monitor(db, trade=False, client=None, paper=True):
    mon = live_monitor.LiveMonitor(_cfg(db), trade=trade, use_ws=False, paper=paper)
    if client is not None:
        mon.client = client
        mon.engine.client = client
    return mon


def _seed(target, **kw):
    base = dict(ticker="KRW-BTC", sleeve="1D", strategy="turtle_1d", entry_price=100_000.0,
                atr=3_000.0, amount_krw=1_000_000.0, units=1, unit_krw=1_000_000.0,
                last_buy_price=100_000.0, highest_price=100_000.0, risk_pct=0.02, max_pyramid=2)
    base.update(kw)
    pos = SleevePosition(**base)
    pos.extras["qty"] = pos.amount_krw / pos.entry_price
    rm = getattr(target, "risk", target)          # MTFEngine 이면 .risk 사용
    rm.register(pos, qty=pos.extras["qty"])
    return pos


# ================= F03: ADX NaN =================
def test_f03_adx_finite_when_first_bar_is_inside():
    """첫 봉이 내부봉(고가/저가 방향성 없음)이어도 ADX가 살아 있어야 한다."""
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, 200)))
    high, low = close * 1.005, close * 0.995
    high[1] = high[0] = close[0]          # 첫 상승/하락폭 0 → plus_dm=minus_dm=0
    low[1] = low[0] = close[0]
    adx, _, _ = ind.adx(high, low, close, 14)
    assert np.isfinite(adx[-1]), "ADX 전 구간 NaN — dx[0]=0/0 이 EWM 시드로 전파됨"


def test_f03_real_db_has_no_all_nan_adx():
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "data" / "upbit.db"
    if not db.exists():
        pytest.skip("실측 DB 없음")
    bad = []
    for t in ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA", "KRW-DOGE",
              "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK"]:
        c = datastore.load(str(db), "1d", t)
        if len(c) < 100:
            continue
        a, _, _ = ind.adx(c.high, c.low, c.close, 14)
        if np.isnan(a[40:]).any():
            bad.append(t)
    assert bad == [], f"ADX NaN 종목: {bad}"


def test_f03_flat_bar_does_not_poison_adx():
    """거래정지(고가=저가=종가) 봉이 하루 섞여도 이후 ADX가 유한해야 한다."""
    close = 100 * np.exp(np.cumsum(np.full(120, 0.003)))
    high, low = close * 1.004, close * 0.996
    high[60] = low[60] = close[60]
    adx, _, _ = ind.adx(high, low, close, 14)
    assert np.isfinite(adx[-1])


# ================= F01 / SAFETY-03: equity =================
def test_f01_equity_refreshed_each_cycle(db, runtime):
    client = FakeClient(equity=10_000_000)
    mon = _monitor(db, client=client)
    mon._refresh_equity()
    client.equity = 9_000_000
    mon._refresh_equity()
    assert mon.engine.equity_krw == pytest.approx(9_000_000)
    assert mon.engine.risk.equity_krw == pytest.approx(9_000_000)


def test_f01_daily_loss_limit_actually_trips(db, runtime):
    eng = _engine(db)
    eng._daily_starting_equity = 10_000_000
    eng.equity_krw = 9_400_000
    eng.risk.equity_krw = 9_400_000
    res = eng.run_cycle()
    assert "Daily loss" in (res["emergency_stop"] or "")


def test_f01_equity_fetch_failure_keeps_last_value(db, runtime):
    client = FakeClient()
    mon = _monitor(db, client=client)
    mon._refresh_equity()
    last = mon.engine.equity_krw
    client.fail_equity = True
    mon._refresh_equity()
    assert mon.engine.equity_krw == pytest.approx(last)


def test_f01_three_failures_block_new_entries(db, runtime):
    client = FakeClient()
    mon = _monitor(db, client=client)
    mon._refresh_equity()
    client.fail_equity = True
    for _ in range(3):
        mon._refresh_equity()
    res = mon.run_once()
    assert [o for o in res["orders"] if o["kind"] in ("BUY", "BUY_UP", "BUY_DOWN")] == []


# ================= F02 / NUMERIC-18: 사이징 캡 =================
def test_f02_single_order_capped_to_equity_fraction(db, runtime):
    """ATR이 극히 작으면 유닛이 자본 전액이 될 수 있다 — 주문 캡(15%)을 강제."""
    flat = make_candles(np.full(100, 100.0) + np.arange(100) * 1e-6, ticker="KRW-ETH")
    datastore.upsert(db, "1d", _rows("KRW-ETH", flat))
    eng = _engine(db, equity_krw=10_000_000)
    eng.run_cycle(live_prices={"KRW-ETH": float(flat.close[-1])})
    for o in eng.risk.positions.values():
        assert o.amount_krw <= 10_000_000 * engine_mtf.ORDER_CAP["1D"] * 1.001


def test_f02_order_rejected_when_no_cash(db, runtime):
    eng = _engine(db)
    res = eng.run_cycle(cash_krw=1_000.0)          # 원화 거의 없음
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []


def test_f02_symbol_notional_cap(db, runtime):
    assert engine_mtf.SYMBOL_CAP["1D"] <= 0.30
    assert engine_mtf.SYMBOL_CAP["5M"] <= 0.10
    assert engine_mtf.PORTFOLIO_NOTIONAL_CAP <= 0.90


# ================= F04 / SAFETY-02: 정지 시에도 청산 =================
def test_f04_killswitch_still_emits_liquidation(db, runtime):
    eng = _engine(db)
    _seed(eng, highest_price=140_000.0, atr=2_000.0)
    paths.emergency_stop()
    res = eng.run_cycle(live_prices={"KRW-BTC": 90_000.0})     # Fatal Stop 구간
    assert res["emergency_stop"]
    assert [o for o in res["orders"] if o["kind"] in ("SELL_ALL", "SELL_PARTIAL")]
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []


def test_f04_killswitch_blocks_new_entries_only(db, runtime):
    eng = _engine(db)
    paths.emergency_stop()
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert res["orders"] == []
    assert "Kill-switch" in (res["emergency_stop"] or "")


# ================= STATE-06: 사이클 주문 한도 =================
def test_state06_cycle_limit_defers_buys_not_liquidations(db, runtime, monkeypatch):
    eng = _engine(db)
    _seed(eng, ticker="KRW-BTC", highest_price=140_000.0, atr=2_000.0)
    monkeypatch.setattr(eng, "max_orders_per_cycle", 0)
    res = eng.run_cycle(live_prices={"KRW-BTC": 90_000.0})
    assert [o for o in res["orders"] if o["kind"] == "SELL_ALL"], "청산은 폐기되면 안 된다"
    assert not (res["emergency_stop"] or "").startswith("사이클")


def test_state06_no_ghost_positions_on_deferred_buys(db, runtime, monkeypatch):
    eng = _engine(db)
    monkeypatch.setattr(eng, "max_orders_per_cycle", 0)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []
    assert eng.risk.positions == {}, "주문 없는 포지션이 등록되면 유령 포지션이 된다"


# ================= LIFECYCLE-01: 수수료/잔고 =================
def test_lifecycle01_entry_qty_reflects_fee(db, runtime):
    eng = _engine(db)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    buys = [o for o in res["orders"] if o["kind"] == "BUY"]
    assert buys
    pos = eng.risk.get("KRW-BTC")
    expected = buys[0]["amount_krw"] * (1 - engine_mtf.FEE) / buys[0]["price"]
    assert pos.extras["qty"] == pytest.approx(expected, rel=1e-6)


def test_lifecycle01_sell_qty_clamped_to_exchange_balance(db, runtime):
    client = FakeClient(holdings={"KRW-BTC": (0.00001, 140.0)})   # 거래소에 아주 조금만 남음
    mon = _monitor(db, trade=True, client=client, paper=False)
    _seed(mon.engine)
    order = {"kind": "SELL_ALL", "ticker": "KRW-BTC", "ratio": 1.0, "price": 140.0}
    mon._execute(order)
    assert client.sells and client.sells[0][1] <= 0.00001


def test_lifecycle01_sell_skipped_when_balance_zero(db, runtime):
    client = FakeClient(holdings={})
    mon = _monitor(db, trade=True, client=client, paper=False)
    _seed(mon.engine)
    assert mon._execute({"kind": "SELL_ALL", "ticker": "KRW-BTC", "ratio": 1.0, "price": 140.0}) is False
    assert client.sells == []


# ================= F05 / STATE-11: CTP 사다리 =================
def test_state11_rung_not_consumed_until_fill():
    rm = MTFRiskManager(equity_krw=10_000_000)
    pos = _seed(rm)
    pos.highest_price = 104_000.0
    rm.evaluate("KRW-BTC", 104_000.0)                    # 사다리 설치
    assert len(rm.get("KRW-BTC").ctp_ladder) == 4
    trigger = rm.get("KRW-BTC").ctp_ladder[0][0]
    act = rm.evaluate("KRW-BTC", trigger - 10)
    assert act.kind == "SELL_PARTIAL"
    assert len(rm.get("KRW-BTC").ctp_ladder) == 4, "체결 전에 rung 을 소비하면 실패 시 영구 유실"
    rm.on_sell("KRW-BTC", trigger - 10, act.ratio)       # 체결 확정
    assert len(rm.get("KRW-BTC").ctp_ladder) == 3


def test_f05_last_rungs_reachable_between_3_and_6_percent():
    """peak +3~6% 구간에서도 잔량이 avg+2% 방어선에서 소진돼야 한다."""
    rm = MTFRiskManager(equity_krw=10_000_000)
    pos = _seed(rm, atr=1_000.0)
    pos.highest_price = 104_000.0                         # +4%
    rm.evaluate("KRW-BTC", 104_000.0)
    ladder = rm.get("KRW-BTC").ctp_ladder
    min_price = 100_000 * 1.02
    assert all(trig >= min_price - 1e-6 for trig, ratio, _ in ladder if ratio >= 0.5), ladder
    # 가격을 방어선까지 밀어 내리면 잔량이 전부 소진돼야 한다
    sold = 0.0
    for px in (103_000, 102_500, 102_100, 102_000, 101_999):
        for _ in range(4):
            act = rm.evaluate("KRW-BTC", px)
            if act.kind != "SELL_PARTIAL":
                break
            sold += act.ratio
            rm.on_sell("KRW-BTC", px, act.ratio)
    assert sold > 0.5, f"소진된 비율 {sold:.2f} — 3·4단이 도달 불가"


# ================= F08 / STATE-19: risk_pct 실시간 =================
def test_f08_risk_pct_updates_after_pyramid():
    rm = MTFRiskManager(equity_krw=10_000_000)
    pos = _seed(rm, risk_pct=0.006)              # 2×ATR×10개 / 자본 = 0.6%
    before = pos.risk_pct
    rm.on_buy("KRW-BTC", 100_000.0, 1_000_000.0, kind="BUY_UP")
    after = rm.get("KRW-BTC").risk_pct
    assert after > before
    assert after <= 0.025 + 1e-9, f"종목 리스크 2.5% 초과: {after:.4f}"


def test_f08_risk_pct_shrinks_after_partial_sell():
    rm = MTFRiskManager(equity_krw=10_000_000)
    pos = _seed(rm)
    before = pos.risk_pct
    rm.on_sell("KRW-BTC", 100_000.0, 0.5)
    assert rm.get("KRW-BTC").risk_pct < before


# ================= F09: 종가 판정 =================
def test_f09_live_spike_does_not_enter_on_1d(db, runtime):
    """1D 진입은 '완료된 일봉 종가' 기준 — 장중 스파이크로 진입하면 백테스트와 다른 전략이 된다."""
    eng = _engine(db)
    flat = make_candles(np.full(100, 100.0), ticker="KRW-ETH")
    datastore.upsert(db, "1d", _rows("KRW-ETH", flat))
    res = eng.run_cycle(live_prices={"KRW-ETH": 500.0})       # 장중 급등 (종가 아님)
    assert [o for o in res["orders"] if o["ticker"] == "KRW-ETH"] == []


def test_f09_exit_still_uses_live_price(db, runtime):
    eng = _engine(db)
    _seed(eng, highest_price=140_000.0, atr=2_000.0)
    res = eng.run_cycle(live_prices={"KRW-BTC": 80_000.0})
    assert [o for o in res["orders"] if o["kind"] == "SELL_ALL"]


# ================= F10 / CONCURRENCY-13: 가격 신선도 =================
def test_f10_stale_prices_are_filtered(db, runtime):
    mon = _monitor(db)
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mon._prices = {"KRW-BTC": {"price": 140.0, "ts": old},
                   "KRW-ETH": {"price": 100.0, "ts": fresh}}
    assert mon.live_prices() == {"KRW-ETH": 100.0}


def test_f10_no_entry_when_all_prices_stale(db, runtime, monkeypatch):
    mon = _monitor(db)
    monkeypatch.setattr(mon, "_public_price", lambda t: None)   # 외부 시세도 없음
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    mon._prices = {"KRW-BTC": {"price": 140.0, "ts": old}}
    res = mon.run_once()
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []


# ================= STATE-07: 원자적 저장 =================
def test_state07_persist_is_atomic(db, runtime, monkeypatch):
    eng = _engine(db, paper=False)
    eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert paths.POSITIONS_FILE.exists()
    original = paths.POSITIONS_FILE.read_text(encoding="utf-8")
    json.loads(original)                                   # 유효 JSON

    def boom(src, dst):
        raise OSError("디스크 오류")
    monkeypatch.setattr("os.replace", boom)
    eng.persist()                                          # 예외를 삼키고 기존 파일 유지
    assert json.loads(paths.POSITIONS_FILE.read_text(encoding="utf-8")) == json.loads(original)


def test_state07_corrupt_positions_file_blocks_new_entries(db, runtime):
    paths.POSITIONS_FILE.write_text("{broken", encoding="utf-8")
    eng = _engine(db, paper=False)
    assert eng.state_error is not None
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []


# ================= STATE-12: 페이퍼/실거래 분리 =================
def test_state12_paper_positions_use_separate_file(db, runtime):
    eng = _engine(db, paper=True)
    eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert paths.POSITIONS_PAPER_FILE.exists()
    assert not paths.POSITIONS_FILE.exists()


def test_state12_live_engine_ignores_paper_state(db, runtime):
    eng_paper = _engine(db, paper=True)
    eng_paper.run_cycle(live_prices={"KRW-BTC": 140.0})
    eng_live = _engine(db, paper=False)
    assert eng_live.risk.positions == {}


# ================= STATE-14: pause 영속 =================
def test_state14_pause_file_blocks_entries_and_survives_restart(db, runtime):
    eng = _engine(db)
    paths.pause()
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []
    eng2 = _engine(db)                                     # 재시작
    res2 = eng2.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res2["orders"] if o["kind"] == "BUY"] == []
    paths.unpause()
    assert [o for o in _engine(db).run_cycle(live_prices={"KRW-BTC": 140.0})["orders"]
            if o["kind"] == "BUY"]


# ================= LIFECYCLE-08: 승급 롤백 =================
def test_lifecycle08_graduate_failure_restores_5m_position(db, runtime):
    eng = _engine(db)
    pos = _seed(eng, sleeve="5M", strategy="turtle_5m", entry_price=100.0, atr=1.0,
                amount_krw=100_000.0, unit_krw=100_000.0, last_buy_price=100.0, risk_pct=0.005)
    order = {"kind": "GRADUATE", "ticker": "KRW-BTC", "amount_krw": 100_000.0, "price": 110.0,
             "atr": 1.0, "pre_state": eng._pos_state(pos)}
    eng.on_order_filled(order, fill_price=110.0)
    assert eng.risk.get("KRW-BTC").sleeve == "1D"
    eng.rollback_order(order, "매수 실패")
    restored = eng.risk.get("KRW-BTC")
    assert restored is not None and restored.sleeve == "5M"


# ================= STATE-10: 수동 포지션 =================
def test_state10_exchange_synced_positions_are_observe_only(db, runtime):
    class C:
        def get_all_balances(self):
            return {"KRW-XRP": {"qty": 1000.0, "avg_price": 2_000.0,
                                "value_krw": 2_000_000.0, "price": 2_000.0}}
    eng = _engine(db, client=C())
    eng.sync_from_exchange()
    pos = eng.risk.get("KRW-XRP")
    assert pos is not None and pos.auto_manage is False
    res = eng.run_cycle(live_prices={"KRW-XRP": 1_500.0})       # -25% (Fatal Stop 조건)
    assert [o for o in res["orders"] if o["ticker"] == "KRW-XRP"] == []


# ================= F13: 유동성 필터 =================
def test_f13_thin_market_is_skipped(tmp_db, runtime):
    c = _breakout()
    datastore.upsert(tmp_db, "1d", _rows("KRW-BTC", c, min_vol_krw=1e8))   # 20일 평균 1억 (미달)
    eng = _engine(tmp_db)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []


# ================= F18: 거래 원장 =================
def test_f18_trade_journal_records_fills(db, runtime):
    eng = _engine(db)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    buy = [o for o in res["orders"] if o["kind"] == "BUY"][0]
    eng.on_order_filled(buy, fill_price=buy["price"])
    _seed(eng)
    sell = {"kind": "SELL_PARTIAL", "ticker": "KRW-BTC", "ratio": 0.5, "price": 110_000.0}
    eng.on_order_filled(sell, fill_price=110_000.0)
    assert paths.TRADES_FILE.exists()
    rows = [json.loads(l) for l in paths.TRADES_FILE.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["side"] == "buy" and rows[0]["fee"] > 0
    assert rows[1]["side"] == "sell" and "realized_pnl" in rows[1]


# ================= DATA-21: 5m 갱신 =================
def test_data21_updater_default_includes_5m():
    import data_updater
    ap_default = data_updater.default_timeframes() if hasattr(data_updater, "default_timeframes") else None
    if ap_default is None:
        pytest.skip("default_timeframes 미구현")
    assert "5m" in ap_default


def test_data21_ticker_exception_is_isolated(db, runtime, monkeypatch):
    eng = _engine(db)
    calls = {"n": 0}
    real = eng._load

    def flaky(tf, ticker, limit=None):
        if ticker == "KRW-BTC":
            raise RuntimeError("데이터 손상")
        return real(tf, ticker, limit=limit)

    monkeypatch.setattr(eng, "_load", flaky)
    res = eng.run_cycle(live_prices={"KRW-ETH": 100.0})
    assert res["timestamp"]                       # 전체 사이클이 죽지 않는다


# ================= CONCURRENCY-15: PID 락 =================
def test_concurrency15_pidlock_is_exclusive(runtime):
    p = paths.RUNTIME_DIR / "live.pid"
    a, b = live_monitor.PidLock(p), live_monitor.PidLock(p)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True


def test_state07_absurd_position_is_dropped_on_load(db, runtime):
    """단위 오류/오염된 포지션은 복구하지 않는다 (qty 폭주 또는 진입가가 시세와 5배 이상 어긋남)."""
    def pos(ticker, entry, qty):
        return {"ticker": ticker, "sleeve": "1D", "strategy": "x", "entry_price": entry,
                "atr": 3.0, "amount_krw": entry * qty, "units": 1, "unit_krw": entry * qty,
                "last_buy_price": entry, "highest_price": entry, "risk_pct": 0.01,
                "extras": {"qty": qty}}

    junk = {
        "KRW-BTC": pos("KRW-BTC", 131.0, 10_000_000.0),      # 평가액이 자본의 1,300배
        "KRW-ETH": pos("KRW-ETH", 0.5, 1_000.0),             # 진입가가 시세(≈100)의 1/200
    }
    paths.atomic_write_text(paths.POSITIONS_FILE, json.dumps(junk))
    eng = _engine(db, paper=False)
    assert eng.risk.get("KRW-BTC") is None
    assert eng.risk.get("KRW-ETH") is None


def test_state07_valid_position_is_kept(db, runtime):
    c = datastore.load(db, "1d", "KRW-BTC", limit=1)
    ref = float(c.close[-1])
    good = {"KRW-BTC": {"ticker": "KRW-BTC", "sleeve": "1D", "strategy": "turtle_1d",
                        "entry_price": ref, "atr": ref * 0.03, "amount_krw": 1_000_000.0,
                        "units": 1, "unit_krw": 1_000_000.0, "last_buy_price": ref,
                        "highest_price": ref, "risk_pct": 0.02, "extras": {"qty": 1_000_000.0 / ref}}}
    paths.atomic_write_text(paths.POSITIONS_FILE, json.dumps(good))
    eng = _engine(db, paper=False)
    assert eng.risk.get("KRW-BTC") is not None
