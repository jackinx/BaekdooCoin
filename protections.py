# -*- coding: utf-8 -*-
"""보호장치 (Freqtrade protections 대응) — 감사 F04/벤치마크 갭.

- StoplossGuard: 24h 내 손절 4회 → 4시간 신규 진입 전면 중단
- DrawdownGuard: 30일 고점 대비 -20% → 12시간 중단
- Cooldown: 청산 후 2시간 재진입 금지 (종목 단위는 TradeLimiter 가 담당)

상태는 파일로 영속화되어 재시작 후에도 유지된다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import paths

log = logging.getLogger("baekdoocoin.protections")


class Protections:
    def __init__(self, stoploss_count: int = 4, stoploss_window_h: int = 24,
                 stoploss_halt_h: int = 4, drawdown_pct: float = 0.20,
                 drawdown_window_d: int = 30, drawdown_halt_h: int = 12):
        self.stoploss_count = stoploss_count
        self.stoploss_window = timedelta(hours=stoploss_window_h)
        self.stoploss_halt = timedelta(hours=stoploss_halt_h)
        self.drawdown_pct = drawdown_pct
        self.drawdown_window = timedelta(days=drawdown_window_d)
        self.drawdown_halt = timedelta(hours=drawdown_halt_h)
        self.stops: List[datetime] = []
        self.equity_log: List[Tuple[datetime, float]] = []
        self.halt_until: Optional[datetime] = None
        self.halt_reason: Optional[str] = None
        self.load()

    # ---------------- 영속화 ----------------
    @property
    def path(self):
        return paths.RUNTIME_DIR / "protections.json"

    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.stops = [datetime.fromisoformat(x) for x in d.get("stops", [])]
            self.equity_log = [(datetime.fromisoformat(a), float(b)) for a, b in d.get("equity", [])]
            hu = d.get("halt_until")
            self.halt_until = datetime.fromisoformat(hu) if hu else None
            self.halt_reason = d.get("halt_reason")
        except Exception as e:                       # noqa: BLE001
            log.warning("보호장치 상태 로드 실패: %s", e)

    def save(self) -> None:
        try:
            paths.atomic_write_text(self.path, json.dumps({
                "stops": [x.isoformat() for x in self.stops[-50:]],
                "equity": [[a.isoformat(), b] for a, b in self.equity_log[-500:]],
                "halt_until": self.halt_until.isoformat() if self.halt_until else None,
                "halt_reason": self.halt_reason,
            }, ensure_ascii=False, indent=2))
        except Exception as e:                       # noqa: BLE001
            log.warning("보호장치 상태 저장 실패: %s", e)

    # ---------------- 기록 ----------------
    def record_stop(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.stops.append(now)
        self.stops = [s for s in self.stops if now - s <= self.stoploss_window]
        if len(self.stops) >= self.stoploss_count:
            self._halt(now, self.stoploss_halt, f"StoplossGuard({len(self.stops)}회/{self.stoploss_window})")
        self.save()

    def record_equity(self, equity_krw: float, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.equity_log.append((now, float(equity_krw)))
        self.equity_log = [(a, b) for a, b in self.equity_log if now - a <= self.drawdown_window]
        peak = max((b for _, b in self.equity_log), default=equity_krw)
        if peak > 0:
            dd = 1 - equity_krw / peak
            if dd >= self.drawdown_pct:
                self._halt(now, self.drawdown_halt, f"DrawdownGuard(-{dd * 100:.1f}%)")
        self.save()

    def _halt(self, now: datetime, duration: timedelta, reason: str) -> None:
        self.halt_until = now + duration
        self.halt_reason = reason
        log.error("🛡️ 보호장치 발동: %s — %s 까지 신규 진입 중단", reason,
                  self.halt_until.strftime("%Y-%m-%d %H:%M"))

    # ---------------- 조회 ----------------
    def blocked(self, now: Optional[datetime] = None) -> Optional[str]:
        now = now or datetime.now(timezone.utc)
        if self.halt_until and now < self.halt_until:
            return f"{self.halt_reason} (~{self.halt_until.astimezone(timezone.utc).strftime('%H:%M')}Z)"
        return None
