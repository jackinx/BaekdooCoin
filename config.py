# -*- coding: utf-8 -*-
""".env 설정 로더 (python-dotenv 없이도 동작. 우선순위: 실제 환경변수 > .env)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent

# 원본 MAJOR_10_TICKERS 와 동일
MAJOR_10_TICKERS: List[str] = [
    "KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA",
    "KRW-DOGE", "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK",
]


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        out[key.strip()] = val
    return out


def _get(env: Dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env.get(key) or default


def _num(env: Dict[str, str], key: str, default: float, cast=float, low=None, high=None) -> float:
    """환경변수 숫자 파싱 — 오타('1,000,000') 때문에 봇이 못 뜨는 일을 막는다."""
    raw = _get(env, key)
    if raw == "":
        return cast(default)
    try:
        val = cast(str(raw).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        print(f"[config] ⚠️ {key}='{raw}' 파싱 실패 → 기본값 {default} 사용", file=sys.stderr)
        return cast(default)
    if low is not None and val < low:
        print(f"[config] ⚠️ {key}={val} < 하한 {low} → 하한 적용", file=sys.stderr)
        val = cast(low)
    if high is not None and val > high:
        print(f"[config] ⚠️ {key}={val} > 상한 {high} → 상한 적용", file=sys.stderr)
        val = cast(high)
    return val


@dataclass
class Config:
    upbit_access_key: str = ""
    upbit_secret_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    db_path: str = ""
    tickers: List[str] = field(default_factory=lambda: list(MAJOR_10_TICKERS))
    initial_equity_krw: float = 1_000_000.0
    start_equity_krw: Optional[float] = None   # 일일 손실 기준선(없으면 현재 총자산)
    max_daily_loss_pct: float = 0.05
    max_daily_trades: int = 30
    max_orders_per_cycle: int = 10
    cycle_seconds: int = 60
    paper: bool = True                          # 기본은 모의(주문 없음)

    @classmethod
    def load(cls, env_path: Optional[Path] = None) -> "Config":
        env = _parse_env_file(env_path or (BASE_DIR / ".env"))
        start_raw = _get(env, "START_EQUITY_KRW")
        cfg = cls(
            upbit_access_key=_get(env, "UPBIT_ACCESS_KEY"),
            upbit_secret_key=_get(env, "UPBIT_SECRET_KEY"),
            telegram_bot_token=_get(env, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get(env, "TELEGRAM_CHAT_ID"),
            db_path=_get(env, "DB_PATH", str(BASE_DIR / "data" / "upbit.db")),
            initial_equity_krw=_num(env, "INITIAL_EQUITY_KRW", 1_000_000, low=10_000),
            start_equity_krw=_num(env, "START_EQUITY_KRW", 0, low=0) or None
            if start_raw else None,
            max_daily_loss_pct=_num(env, "MAX_DAILY_LOSS_PCT", 0.05, low=0.005, high=0.5),
            max_daily_trades=int(_num(env, "MAX_DAILY_TRADES", 30, low=1, high=1000)),
            max_orders_per_cycle=int(_num(env, "MAX_ORDERS_PER_CYCLE", 10, low=1, high=50)),
            cycle_seconds=int(_num(env, "CYCLE_SECONDS", 60, low=10, high=3600)),
        )
        t = _get(env, "TICKERS")
        if t:
            cfg.tickers = [x.strip().upper() for x in t.split(",") if x.strip()]
        return cfg

    # ---- helpers -------------------------------------------------
    def has_upbit(self) -> bool:
        return bool(self.upbit_access_key and self.upbit_secret_key)

    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def missing_required(self) -> List[str]:
        miss = []
        if not self.has_upbit():
            miss.append("UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY")
        return miss


if __name__ == "__main__":
    cfg = Config.load()
    print("=== 백두코인봇(Windows) 설정 확인 ===")
    print(f"업비트 액세스키 : {'설정됨 OK' if cfg.upbit_access_key else '미설정 X'}")
    print(f"업비트 시크릿키 : {'설정됨 OK' if cfg.upbit_secret_key else '미설정 X'}")
    print(f"텔레그램 토큰   : {'설정됨 OK' if cfg.telegram_bot_token else '미설정 X'}")
    print(f"텔레그램 챗ID   : {'설정됨 OK' if cfg.telegram_chat_id else '미설정 X'}")
    print(f"DB              : {cfg.db_path}")
    print(f"종목({len(cfg.tickers)})     : {', '.join(cfg.tickers)}")
    print(f"사이클          : {cfg.cycle_seconds}s / 일일손실한도 {cfg.max_daily_loss_pct:.0%} / 일일거래 {cfg.max_daily_trades}회")
