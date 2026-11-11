# -*- coding: utf-8 -*-
"""BaekdooCoin (Windows port) — 경로 정의.

원본(macOS)은 /tmp/baekdoocoin_* 를 썼지만 Windows에는 /tmp가 없다.
런타임 상태(킬스위치/포지션/릴레이/로그)는 %LOCALAPPDATA%\\BaekdooCoin 로 보낸다.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

_rt = os.environ.get("BAEKDOOCOIN_RUNTIME_DIR") or os.environ.get("LOCALAPPDATA")
RUNTIME_DIR = Path(_rt) / "BaekdooCoin" if _rt else BASE_DIR / "runtime"

DB_PATH = DATA_DIR / "upbit.db"
POSITIONS_FILE = RUNTIME_DIR / "open_positions.json"
POSITIONS_PAPER_FILE = RUNTIME_DIR / "open_positions_paper.json"
STATE_FILE = RUNTIME_DIR / "engine_state.json"
TRADES_FILE = RUNTIME_DIR / "trades.jsonl"
EMERGENCY_STOP_FILE = RUNTIME_DIR / "EMERGENCY_STOP"
PAUSE_FILE = RUNTIME_DIR / "PAUSED"
RELAY_DIR = RUNTIME_DIR / "relay"
ALERTS_FILE = RELAY_DIR / "alerts.jsonl"
COMMAND_FILE = RELAY_DIR / "command.json"


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, RUNTIME_DIR, RELAY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """tmp 에 쓰고 os.replace 로 교체 — 중간에 죽어도 원본이 깨지지 않는다."""
    ensure_dirs()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def emergency_stop() -> None:
    """킬스위치 ON — 다음 사이클부터 신규 진입/증액 중단(청산은 계속)."""
    ensure_dirs()
    EMERGENCY_STOP_FILE.write_text("stop", encoding="utf-8")


def emergency_clear() -> None:
    try:
        EMERGENCY_STOP_FILE.unlink()
    except FileNotFoundError:
        pass


def emergency_active() -> bool:
    return EMERGENCY_STOP_FILE.exists()


def pause() -> None:
    """일시정지 — 파일로 남겨 재시작 후에도 유지된다."""
    ensure_dirs()
    PAUSE_FILE.write_text("paused", encoding="utf-8")


def unpause() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def paused() -> bool:
    return PAUSE_FILE.exists()
