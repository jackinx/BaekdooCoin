# -*- coding: utf-8 -*-
"""설정 로더 / 경로 단위 테스트."""
from __future__ import annotations

import os

import pytest

import config as cfg_mod
import paths


# ---------------- Config ----------------
def test_loads_from_env_file(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text(
        "UPBIT_ACCESS_KEY=AAA\nUPBIT_SECRET_KEY=BBB\nTELEGRAM_BOT_TOKEN=TT\n"
        "TELEGRAM_CHAT_ID=12345\nMAX_DAILY_TRADES=7\nMAX_DAILY_LOSS_PCT=0.02\n"
        "TICKERS=KRW-BTC, KRW-ETH,,krw-sol\n", encoding="utf-8")
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHAT_ID", "MAX_DAILY_TRADES", "MAX_DAILY_LOSS_PCT", "TICKERS"):
        monkeypatch.delenv(k, raising=False)
    cfg = cfg_mod.Config.load(envf)
    assert cfg.has_upbit() and cfg.has_telegram()
    assert cfg.max_daily_trades == 7
    assert cfg.max_daily_loss_pct == pytest.approx(0.02)
    assert cfg.tickers == ["KRW-BTC", "KRW-ETH", "KRW-SOL"]     # 공백/빈값/소문자 정리


def test_real_env_overrides_file(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text("UPBIT_ACCESS_KEY=FROM_FILE\nUPBIT_SECRET_KEY=x\n", encoding="utf-8")
    monkeypatch.setenv("UPBIT_ACCESS_KEY", "FROM_ENV")
    assert cfg_mod.Config.load(envf).upbit_access_key == "FROM_ENV"


def test_missing_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.delenv("UPBIT_ACCESS_KEY", raising=False)
    cfg = cfg_mod.Config.load(tmp_path / "nope.env")
    assert cfg.upbit_access_key == ""
    assert "UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY" in cfg.missing_required()
    assert cfg.tickers == cfg_mod.MAJOR_10_TICKERS


def test_comments_quotes_and_blanks(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text('# comment\n\nUPBIT_ACCESS_KEY="quoted"\nUPBIT_SECRET_KEY=\'sq\'\n',
                    encoding="utf-8")
    monkeypatch.delenv("UPBIT_ACCESS_KEY", raising=False)
    monkeypatch.delenv("UPBIT_SECRET_KEY", raising=False)
    cfg = cfg_mod.Config.load(envf)
    assert cfg.upbit_access_key == "quoted" and cfg.upbit_secret_key == "sq"


def test_defaults_are_conservative(tmp_path, monkeypatch):
    for k in ("MAX_DAILY_LOSS_PCT", "MAX_DAILY_TRADES", "MAX_ORDERS_PER_CYCLE", "CYCLE_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    cfg = cfg_mod.Config.load(tmp_path / "none.env")
    assert cfg.max_daily_loss_pct <= 0.05
    assert cfg.max_daily_trades <= 30
    assert cfg.max_orders_per_cycle <= 10
    assert cfg.paper is True          # 모의가 기본값


# ---------------- paths / kill switch ----------------
@pytest.fixture()
def sandbox_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path / "rt")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "RELAY_DIR", tmp_path / "rt" / "relay")
    monkeypatch.setattr(paths, "POSITIONS_FILE", tmp_path / "rt" / "open_positions.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "rt" / "engine_state.json")
    monkeypatch.setattr(paths, "EMERGENCY_STOP_FILE", tmp_path / "rt" / "EMERGENCY_STOP")
    return paths


def test_ensure_dirs_creates_all(sandbox_paths):
    sandbox_paths.ensure_dirs()
    assert sandbox_paths.DATA_DIR.is_dir()
    assert sandbox_paths.LOG_DIR.is_dir()
    assert sandbox_paths.RUNTIME_DIR.is_dir()
    assert sandbox_paths.RELAY_DIR.is_dir()


def test_kill_switch_lifecycle(sandbox_paths):
    sandbox_paths.ensure_dirs()
    assert sandbox_paths.emergency_active() is False
    sandbox_paths.emergency_stop()
    assert sandbox_paths.emergency_active() is True
    sandbox_paths.emergency_clear()
    assert sandbox_paths.emergency_active() is False
    sandbox_paths.emergency_clear()               # 두 번 지워도 예외 없음


def test_kill_switch_file_is_outside_project(sandbox_paths):
    """프로세스가 죽어도 살아남아야 하므로 런타임(프로젝트 밖) 디렉터리에 둔다."""
    assert sandbox_paths.EMERGENCY_STOP_FILE.parent == sandbox_paths.RUNTIME_DIR
