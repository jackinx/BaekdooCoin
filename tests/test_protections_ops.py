# -*- coding: utf-8 -*-
"""보호장치 / 거래원장 리포트 / 워치독 단위 테스트."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import paths
import protections as prot_mod
from protections import Protections


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
    paths.ensure_dirs()
    return rt


# ---------------- StoplossGuard ----------------
def test_stoploss_guard_halts_after_four_stops(runtime):
    p = Protections()
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        p.record_stop(now + timedelta(minutes=i))
    assert p.blocked(now + timedelta(minutes=5)) is None
    p.record_stop(now + timedelta(minutes=10))
    blocked = p.blocked(now + timedelta(minutes=11))
    assert blocked and "StoplossGuard" in blocked


def test_stoploss_guard_expires_after_halt_window(runtime):
    p = Protections(stoploss_halt_h=4)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    for i in range(4):
        p.record_stop(now)
    assert p.blocked(now + timedelta(hours=3)) is not None
    assert p.blocked(now + timedelta(hours=5)) is None


def test_old_stops_outside_window_do_not_count(runtime):
    p = Protections(stoploss_window_h=24)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    for i in range(4):
        p.record_stop(now - timedelta(hours=30))
    p.record_stop(now)
    assert p.blocked(now) is None


# ---------------- DrawdownGuard ----------------
def test_drawdown_guard_halts_on_20pct_drop(runtime):
    p = Protections()
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    p.record_equity(1_000_000, now)
    p.record_equity(850_000, now + timedelta(hours=1))
    assert p.blocked(now + timedelta(hours=1)) is None
    p.record_equity(790_000, now + timedelta(hours=2))
    blocked = p.blocked(now + timedelta(hours=2))
    assert blocked and "DrawdownGuard" in blocked


def test_drawdown_guard_ignores_normal_fluctuation(runtime):
    p = Protections()
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    for eq in (1_000_000, 990_000, 1_010_000, 995_000):
        p.record_equity(eq, now)
    assert p.blocked(now) is None


# ---------------- 영속화 ----------------
def test_state_persists_across_restart(runtime):
    now = datetime.now(timezone.utc)
    p = Protections()
    for _ in range(4):
        p.record_stop(now)
    assert p.blocked(now) is not None
    p2 = Protections()
    assert p2.blocked(now) is not None, "재시작하면 보호장치가 풀리면 안 된다"


def test_corrupt_state_file_does_not_crash(runtime):
    (paths.RUNTIME_DIR / "protections.json").write_text("{broken", encoding="utf-8")
    p = Protections()
    assert p.blocked() is None


# ---------------- 엔진 연동 ----------------
def test_engine_blocks_entries_when_guard_active(tmp_db, runtime, monkeypatch):
    import datastore
    from engine_mtf import MTFEngine
    from tests.conftest import make_candles
    import numpy as np

    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, 90)
    close = np.concatenate([base, [base.max() * 1.25]])
    high = np.concatenate([base * 1.005, [base.max() * 1.26]])
    low = np.concatenate([base * 0.995, [base.max() * 1.24]])
    c = make_candles(close, high=high, low=low, ticker="KRW-BTC")
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                    in zip(c.ts, c.open, c.high, c.low, c.close, c.volume,
                                           c.volume_krw)])
    now = datetime.now(timezone.utc)
    p = Protections()
    for _ in range(4):
        p.record_stop(now)

    eng = MTFEngine(["KRW-BTC"], tmp_db, equity_krw=10_000_000)
    eng.prot = p                                   # 발동 상태 주입
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []
    assert "StoplossGuard" in (res["entries_blocked"] or "")


# ---------------- 리포트 ----------------
def test_report_summary_reads_journal(runtime, capsys):
    rows = [
        {"ts": "2026-11-01T00:00:00+00:00", "mode": "paper", "ticker": "KRW-BTC", "side": "buy",
         "qty": 1.0, "price": 100.0, "notional": 100.0, "fee": 0.05, "realized_pnl": 0.0},
        {"ts": "2026-11-02T00:00:00+00:00", "mode": "paper", "ticker": "KRW-BTC", "side": "sell",
         "qty": 1.0, "price": 120.0, "notional": 120.0, "fee": 0.06, "realized_pnl": 20.0},
        {"ts": "2026-11-03T00:00:00+00:00", "mode": "paper", "ticker": "KRW-BTC", "side": "sell",
         "qty": 1.0, "price": 90.0, "notional": 90.0, "fee": 0.045, "realized_pnl": -10.0},
    ]
    paths.TRADES_FILE.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    sys.argv = ["report.py", "--file", str(paths.TRADES_FILE)]
    import tools.report as report
    assert report.main() == 0
    out = capsys.readouterr().out
    assert "체결 3건" in out and "BTC" in out


def test_report_tax_buckets_by_year(runtime, capsys):
    rows = [{"ts": "2027-01-05T00:00:00+00:00", "side": "sell", "ticker": "KRW-BTC",
             "realized_pnl": 4_000_000.0, "fee": 0.0}]
    paths.TRADES_FILE.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    sys.argv = ["report.py", "--file", str(paths.TRADES_FILE), "--tax"]
    import tools.report as report
    report.main()
    out = capsys.readouterr().out
    assert "2027" in out and "330,000" in out        # (400만 - 250만) * 22%


def test_report_handles_empty_journal(runtime, capsys):
    sys.argv = ["report.py", "--file", str(paths.TRADES_FILE)]
    import tools.report as report
    report.main()
    assert "비어 있습니다" in capsys.readouterr().out


# ---------------- 워치독 ----------------
def test_watchdog_flags_missing_pid(runtime, capsys):
    sys.argv = ["watchdog.py", "--quiet"]
    import tools.watchdog as wd
    assert wd.main() == 1
    assert "PID" in capsys.readouterr().out


def test_watchdog_ok_when_state_is_fresh(runtime, capsys):
    (paths.RUNTIME_DIR / "live_monitor.pid").write_text(str(os.getpid()))
    paths.STATE_FILE.write_text("{}", encoding="utf-8")
    sys.argv = ["watchdog.py"]
    import tools.watchdog as wd
    assert wd.main() == 0
    assert "정상" in capsys.readouterr().out


def test_watchdog_flags_stale_state(runtime, capsys):
    (paths.RUNTIME_DIR / "live_monitor.pid").write_text(str(os.getpid()))
    paths.STATE_FILE.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(paths.STATE_FILE, (old, old))
    sys.argv = ["watchdog.py", "--max-age", "600"]
    import tools.watchdog as wd
    assert wd.main() == 1
    out = capsys.readouterr().out
    assert "분 전" in out


def test_engine_uses_injected_clock_for_protections(tmp_db, runtime):
    """리플레이에서 보호장치가 벽시계를 쓰면 과거 시뮬레이션이 즉시 중단된다."""
    import numpy as np
    import datastore
    from datetime import datetime, timezone
    from engine_mtf import MTFEngine
    from tests.conftest import make_candles

    rng = np.random.default_rng(3)
    base = 100 + rng.normal(0, 0.5, 90)
    close = np.concatenate([base, [base.max() * 1.25]])
    high = np.concatenate([base * 1.005, [base.max() * 1.26]])
    low = np.concatenate([base * 0.995, [base.max() * 1.24]])
    c = make_candles(close, high=high, low=low, ticker="KRW-BTC")
    datastore.upsert(tmp_db, "1d", [("KRW-BTC", ts, o, h, l, cl, v, vk) for ts, o, h, l, cl, v, vk
                                    in zip(c.ts, c.open, c.high, c.low, c.close, c.volume,
                                           c.volume_krw)])
    eng = MTFEngine(["KRW-BTC"], tmp_db, equity_krw=10_000_000)
    # 과거 시각을 주입 → 4회 손절 기록을 '그 시점'에 남긴다
    sim_now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    eng.now_fn = lambda: sim_now
    for _ in range(4):
        eng.prot.record_stop(sim_now)
    res = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res["orders"] if o["kind"] == "BUY"] == []
    # 시뮬레이션 시각이 4시간 지나면 해제된다
    eng.now_fn = lambda: sim_now + timedelta(hours=5)
    res2 = eng.run_cycle(live_prices={"KRW-BTC": 140.0})
    assert [o for o in res2["orders"] if o["kind"] == "BUY"]
