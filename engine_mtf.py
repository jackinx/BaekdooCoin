# -*- coding: utf-8 -*-
"""MTF 트레이딩 엔진 (원본 engine_mtf.py).

1분 사이클:
  0. 비상정지 체크 (킬스위치 파일 / 플래그 / 일일손실 / 일일거래수)
  1. 티커 순회
     a. 1H 레짐 감지 (DC55 돌파 override 포함)
     b. 포지션 보유 → 풀 매니지먼트 (Fatal Stop / CTP / 불타기 / 물타기 / 1D 엑시트)
     c. 미보유 → 신규 진입 (1D 우선 → 5M 7필터)
     d. 매 진입마다 리스크캡 승인
  2. 스냅샷 + 주문 목록 반환 (실제 주문 실행은 live_monitor 담당)

엔진은 주문을 '제안'하고 포지션 상태를 낙관적으로 반영한다.
주문 실패 시 live_monitor 가 rollback_order() 로 되돌린다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

import datastore
import paths
from protections import Protections
from strategies.regime_detector import MarketRegime, RegimeDetector
from strategies.risk_mtf import Action, MTFRiskManager, SleevePosition
from strategies.turtle_mtf import (PORTFOLIO_TOTAL_CAP, SLEEVE_1D, SLEEVE_5M,
                                   TradeLimiter, TurtleMTF, calc_position_size)

log = logging.getLogger("baekdoocoin.engine")

MIN_ORDER_KRW = 5_000.0
FEE = 0.0005                       # 업비트 편도 수수료 0.05%

# ---- 노출/사이징 상한 (감사 F02 / NUMERIC-18) ----
ORDER_CAP = {"1D": 0.15, "5M": 0.05}              # 주문 1건 ≤ 자본의 15% / 5%
SYMBOL_CAP = {"1D": 0.25, "5M": 0.08}             # 종목 총노출 ≤ 자본의 25% / 8%
SLEEVE_NOTIONAL_CAP = {"1D": 0.60, "5M": 0.15}    # 슬리브 총노출
PORTFOLIO_NOTIONAL_CAP = 0.80                     # 전체 총노출
MIN_VOL_FLOOR = {"1D": 0.02, "5M": 0.0015}        # ATR/가격 하한(변동성 과소 방지)
MIN_VOLUME_KRW = {"1D": 5_000_000_000.0, "5M": 100_000_000.0}   # 20봉 평균 거래대금 하한
SYMBOL_RISK_CAP = 0.025                            # 종목당 리스크 상한


class MTFEngine:
    def __init__(self, tickers: List[str], db_path: str, equity_krw: float = 1_000_000.0,
                 client=None, max_daily_loss_pct: float = 0.05, max_daily_trades: int = 30,
                 max_orders_per_cycle: int = 10, paper: bool = False,
                 start_equity_krw: Optional[float] = None):
        self.tickers = list(tickers)
        self.db_path = str(db_path)
        self.client = client
        self.equity_krw = float(equity_krw)
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_daily_trades = max_daily_trades
        self.max_orders_per_cycle = max_orders_per_cycle
        self.paper = bool(paper)
        self.positions_file = paths.POSITIONS_PAPER_FILE if self.paper else paths.POSITIONS_FILE

        self.risk = MTFRiskManager(equity_krw=self.equity_krw)
        self.regime = RegimeDetector()
        self.turtle_1d = TurtleMTF(SLEEVE_1D)
        self.turtle_5m = TurtleMTF(SLEEVE_5M)
        self.limiter = TradeLimiter()
        self.prot = Protections()

        self._emergency_stop_active = False
        self._daily_starting_equity = float(start_equity_krw or equity_krw)
        self._daily_trades_count = 0
        self._daily_date = datetime.now(timezone.utc).date()
        self._last_cycle: Optional[dict] = None
        self.as_of: Optional[str] = None      # 백테스트/리플레이용: 이 날짜 이후 캔들 무시
        self.state_error: Optional[str] = None
        self.external_block: Optional[str] = None   # equity 조회 실패 등 외부 사유
        self._cycle_cash: Optional[float] = None     # 사이클 내 가용 원화 (run_cycle 에서 설정)
        self.now_fn = lambda: datetime.now(timezone.utc)   # 리플레이에서 시각 주입용

        paths.ensure_dirs()
        self._load_positions()
        self._load_state()

    # ---------------- 상태/영속화 ----------------
    def _load_positions(self) -> None:
        path = self.positions_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.risk.load_json(data)
            # sanity: 자본 대비 말도 안 되는 포지션(오염/단위오류)은 버린다
            dropped = []
            for t, pos in list(self.risk.positions.items()):
                value = pos.extras.get("qty", 0.0) * max(pos.entry_price, 0.0)
                ref = None
                for tf in ("5m", "1h", "1d"):
                    c = self._load(tf, t, limit=1)
                    if c is not None and len(c):
                        ref = float(c.close[-1])
                        break
                bad = (pos.entry_price <= 0 or pos.extras.get("qty", 0) <= 0
                       or value > max(self.equity_krw, 1.0) * 3)
                if not bad and ref and ref > 0:
                    ratio = pos.entry_price / ref
                    bad = ratio > 5 or ratio < 0.2      # 진입가가 시세와 5배 이상 어긋나면 오염
                if bad:
                    dropped.append(f"{t}(진입 {pos.entry_price:,.2f}/시세 {ref or 0:,.2f})")
                    self.risk.positions.pop(t, None)
            if dropped:
                log.error("⚠️ 비정상 포지션 폐기: %s (자본 %.0f)", ", ".join(dropped), self.equity_krw)
            if self.risk.positions:
                log.info("포지션 복구: %d종 (%s)", len(self.risk.positions),
                         ", ".join(self.risk.positions))
            self.state_error = None
        except Exception as e:                     # noqa: BLE001
            # 파일이 깨졌으면 빈 상태로 '조용히' 진행하지 않는다 — 신규 진입 차단 + 경보 대상
            self.state_error = f"포지션 파일 손상: {e}"
            log.error("포지션 파일 로드 실패: %s — 신규 진입 차단", e)

    def _load_state(self) -> None:
        try:
            if paths.STATE_FILE.exists():
                d = json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
                if d.get("daily_date") == str(datetime.now(timezone.utc).date()):
                    self._daily_starting_equity = float(d.get("daily_starting_equity", self.equity_krw))
                    self._daily_trades_count = int(d.get("daily_trades_count", 0))
                    log.info("일일 상태 복구: 기준자본 %.0f / 거래 %d회",
                             self._daily_starting_equity, self._daily_trades_count)
        except Exception as e:                     # noqa: BLE001
            log.warning("상태 파일 로드 실패: %s", e)

    def persist(self) -> None:
        paths.ensure_dirs()
        try:
            paths.atomic_write_text(
                self.positions_file,
                json.dumps(self.risk.to_json(), ensure_ascii=False, indent=2))
            paths.atomic_write_text(paths.STATE_FILE, json.dumps({
                "daily_date": str(datetime.now(timezone.utc).date()),
                "daily_starting_equity": self._daily_starting_equity,
                "daily_trades_count": self._daily_trades_count,
                "equity_krw": self.equity_krw,
                "paper": self.paper,
            }, ensure_ascii=False, indent=2))
        except Exception as e:                     # noqa: BLE001
            log.warning("상태 저장 실패: %s (기존 파일 유지)", e)

    def sync_from_exchange(self) -> int:
        """재시작 시 업비트 실제 보유자산을 리스크매니저에 등록(수동매수 포함)."""
        if not self.client:
            return 0
        n = 0
        try:
            balances = self.client.get_all_balances()
        except Exception as e:                     # noqa: BLE001
            log.warning("거래소 잔고 동기화 실패: %s", e)
            return 0
        for ticker, info in balances.items():
            if info["qty"] <= 0 or info["value_krw"] < 100 or info["avg_price"] <= 0:
                continue
            known = self.risk.positions.get(ticker)
            if known is not None:
                # 기존 포지션은 거래소 수량과 대조해 drift 를 보정한다 (감사 STATE-10)
                qty_live = float(info["qty"])
                if abs(qty_live - known.extras.get("qty", 0.0)) > max(qty_live * 1e-3, 1e-9):
                    log.warning("%s 수량 불일치: 로컬 %.8f vs 거래소 %.8f → 거래소 기준 보정",
                                ticker, known.extras.get("qty", 0.0), qty_live)
                    known.extras["qty"] = qty_live
                    self.risk._recalc_risk(known)
                continue
            risk_pct = min(info["value_krw"] / max(self.equity_krw, 1.0), 0.02)
            pos = SleevePosition(
                ticker=ticker, sleeve="1D", strategy="exchange-sync",
                entry_price=info["avg_price"], atr=info["avg_price"] * 0.03,
                amount_krw=info["qty"] * info["avg_price"], units=1,
                unit_krw=info["qty"] * info["avg_price"], last_buy_price=info["avg_price"],
                highest_price=info["price"], risk_pct=risk_pct, max_pyramid=2,
                auto_manage=False,       # 수동/외부 보유분은 자동 청산·증액 대상에서 제외
                opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self.risk.register(pos, qty=info["qty"])
            n += 1
        if n:
            self.persist()
            log.info("exchange sync: %d positions loaded", n)
        return n

    def register_manual_position(self, ticker: str, entry_price: float, amount_krw: float,
                                 strategy: str = "manual", atr: Optional[float] = None) -> SleevePosition:
        atr = atr or entry_price * 0.03
        pos = SleevePosition(
            ticker=ticker, sleeve="1D", strategy=strategy, entry_price=entry_price, atr=atr,
            amount_krw=amount_krw, units=1, unit_krw=amount_krw, last_buy_price=entry_price,
            highest_price=entry_price,
            risk_pct=(2 * atr / entry_price) * (amount_krw / max(self.equity_krw, 1.0)),
            max_pyramid=2, opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.risk.register(pos)
        self.persist()
        return pos

    # ---------------- 안전장치 ----------------
    def _check_emergency_stop(self) -> Optional[str]:
        if paths.emergency_active():
            return "Kill-switch 파일"
        if self._emergency_stop_active:
            return "Emergency stop active"
        if self._daily_starting_equity > 0:
            loss_pct = (1 - self.equity_krw / self._daily_starting_equity) * 100
            if loss_pct >= self.max_daily_loss_pct * 100:
                return f"Daily loss limit: -{loss_pct:.1f}%"
        if self._daily_trades_count >= self.max_daily_trades:
            return f"Daily trade limit ({self._daily_trades_count})"
        return None

    def stop(self, reason: str = "manual") -> None:
        self._emergency_stop_active = True
        log.warning("비상정지 ON (%s)", reason)

    def resume(self) -> None:
        self._emergency_stop_active = False
        paths.emergency_clear()
        log.warning("비상정지 해제 — 매매 재개")

    def _roll_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_starting_equity = self.equity_krw
            self._daily_trades_count = 0
            log.info("일일 카운터 리셋 (기준자본 %s KRW)", f"{self.equity_krw:,.0f}")

    # ---------------- 데이터 ----------------
    def _load(self, tf: str, ticker: str, limit: Optional[int] = None):
        # as_of(리플레이)면 SQL 레벨에서 그 날짜까지만 잘라 가져온다
        c = datastore.load(self.db_path, tf, ticker, limit=limit,
                           until=(self.as_of + "T23:59:59") if self.as_of else None)
        if len(c) == 0:
            log.debug("데이터 없음: ticker=%s timeframe=%s", ticker, tf)
            return None
        return c

    # ---------------- 메인 사이클 ----------------
    def run_cycle(self, live_prices: Optional[Dict[str, float]] = None,
                  cash_krw: Optional[float] = None,
                  blocked_tickers: Optional[set] = None) -> dict:
        """1 사이클.

        live_prices     : 실시간가(모니터가 신선도 필터 후 전달)
        cash_krw        : 가용 원화 (신규 주문 상한)
        blocked_tickers : 가격을 신뢰할 수 없는 종목 → 주문 금지
        """
        live_prices = live_prices or {}
        blocked = set(blocked_tickers or ())
        self._roll_daily()
        self.risk.equity_krw = self.equity_krw
        orders: List[dict] = []
        regimes: Dict[str, str] = {}
        notes: List[str] = []

        stop_reason = self._check_emergency_stop()
        now = self.now_fn()
        try:
            self.prot.record_equity(self.equity_krw, now=now)
        except Exception as e:                       # noqa: BLE001
            log.warning("보호장치 자본 기록 실패: %s", e)
        block_reason = (stop_reason or self.state_error or self.external_block
                        or self.prot.blocked(now))
        if block_reason is None and paths.paused():
            block_reason = "일시정지"
        allow_new = block_reason is None
        self._cycle_cash = float(cash_krw) if cash_krw is not None else None

        for ticker in self.tickers:
            try:
                self._cycle_ticker(ticker, live_prices, blocked, allow_new, orders, notes, regimes)
            except Exception as e:                       # noqa: BLE001
                log.exception("티커 처리 오류: %s", ticker)
                notes.append(f"{ticker} 처리 오류: {e}")

        # 주문 우선순위: 청산은 절대 폐기하지 않는다. 신규/증액만 한도로 이월.
        sell_kinds = ("SELL_ALL", "SELL_PARTIAL")
        sells = [o for o in orders if o["kind"] in sell_kinds]
        others = [o for o in orders if o["kind"] not in sell_kinds]
        if len(sells) > self.max_orders_per_cycle:
            notes.append(f"⚠️ 청산 주문 {len(sells)}건 > 사이클 한도 {self.max_orders_per_cycle} — 청산 우선 실행")
        budget = max(self.max_orders_per_cycle - len(sells), 0)
        kept, deferred = others[:budget], others[budget:]
        for o in kept:
            if o["kind"] == "BUY":
                self._commit_entry(o)
        for o in deferred:
            notes.append(f"{o['ticker']} {o['kind']} 이월 — 사이클 주문 한도 {self.max_orders_per_cycle}")
        orders = sells + kept

        self._last_cycle = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "equity_krw": self.equity_krw,
            "regimes": regimes,
            "orders": orders,
            "positions": self._snapshot_positions(live_prices),
            "summary": notes,
            "emergency_stop": stop_reason,
            "entries_blocked": block_reason,
        }
        self.persist()
        return self._last_cycle

    def _cycle_ticker(self, ticker: str, live_prices: Dict[str, float], blocked: set,
                      allow_new: bool, orders: List[dict], notes: List[str],
                      regimes: Dict[str, str]) -> None:
        c1h = self._load("1h", ticker, limit=600)
        c1d = self._load("1d", ticker, limit=400)
        c5 = self._load("5m", ticker, limit=600)
        if c1d is None:
            notes.append(f"{ticker} 1D 데이터 없음 — 건너뜀")
            return
        if ticker in blocked:
            notes.append(f"{ticker} 가격 신뢰 불가(스테일) — 주문 금지")
            return

        regime_src = c1h if c1h is not None else c1d
        live_price = live_prices.get(ticker)
        price = float(live_price) if live_price else float(c1d.close[-1])
        regime, _ = self.regime.detect_regime(regime_src, dc_high_override=True)
        regimes[ticker] = regime.value

        pos = self.risk.get(ticker)
        if pos is not None:
            if not pos.auto_manage:
                notes.append(f"{ticker} 수동/동기화 포지션 — 자동관리 제외(관망)")
                return
            self._manage_position(pos, price, regime_src, c1d, c5, orders, notes)
            return

        if not allow_new:
            notes.append(f"{ticker} 신규 진입 차단 — {self.external_block or self.state_error or '비상정지/일시정지'}")
            return
        if regime != MarketRegime.TRENDING:
            return

        # 1D: 완료된 일봉 종가 기준 (장중 스파이크로 진입하지 않는다 — 감사 F09)
        if self._liquidity_ok("1D", c1d):
            sig1d = self.turtle_1d.check_entry(c1d)
            if sig1d.signal == "LONG":
                self._open(ticker, "1D", sig1d, price, orders, notes)
                return
        else:
            notes.append(f"{ticker} 1D 유동성 미달")

        # 5M: 1H 추세 + 7종 필터 (5m/1h 데이터가 모두 있어야 함)
        if c5 is not None and c1h is not None and len(c5) > 60:
            if not self._liquidity_ok("5M", c5):
                notes.append(f"{ticker} 5M 유동성 미달")
                return
            sig5, passed, failed = self.turtle_5m.check_entry_5m(c5, c1h, self.limiter)
            if sig5.signal == "LONG":
                self._open(ticker, "5M", sig5, price, orders, notes)
                return
            if failed:
                notes.append(f"{ticker} 5M 필터 실패({','.join(failed)})")

    # ---------------- 노출 계산 ----------------
    def _symbol_exposure(self, ticker: str) -> float:
        p = self.risk.get(ticker)
        return float(p.amount_krw) if p else 0.0

    def _sleeve_exposure(self, sleeve: str) -> float:
        return sum(p.amount_krw for p in self.risk.positions.values() if p.sleeve == sleeve)

    def _portfolio_exposure(self) -> float:
        return sum(p.amount_krw for p in self.risk.positions.values())

    def _liquidity_ok(self, sleeve: str, c) -> bool:
        if len(c) < 20:
            return False
        try:
            avg = float(np.mean(c.volume_krw[-20:]))
        except Exception:                                # noqa: BLE001
            return False
        return avg >= MIN_VOLUME_KRW[sleeve]

    # ---------------- 포지션 관리 ----------------
    def _manage_position(self, pos: SleevePosition, price: float, c1h, c1d, c5,
                         orders: List[dict], notes: List[str]) -> None:
        strat = self.turtle_1d if pos.sleeve == "1D" else self.turtle_5m
        candles = c1d if pos.sleeve == "1D" else (c5 if c5 is not None else c1h)

        # 1) 리스크 매니저 우선순위 (Fatal Stop → CTP → 불타기 → 물타기)
        act: Action = self.risk.evaluate(pos.ticker, price)
        pre = self._pos_state(pos)          # 롤백용 스냅샷

        if act.kind == "SELL_ALL":
            orders.append({"kind": "SELL_ALL", "ticker": pos.ticker, "ratio": 1.0,
                           "price": price, "reason": act.reason, "sleeve": pos.sleeve,
                           "stop": True, "pre_state": pre})
            notes.append(f"{pos.ticker} SELL_ALL — {act.reason}")
            return
        if act.kind == "SELL_PARTIAL":
            orders.append({"kind": "SELL_PARTIAL", "ticker": pos.ticker, "ratio": act.ratio,
                           "price": price, "reason": act.reason, "sleeve": pos.sleeve,
                           "pre_state": pre})
            notes.append(f"{pos.ticker} SELL {act.ratio:.0%} — {act.reason}")
            return
        if act.kind == "BUY_UP":
            amt = self._cap_buy(act.amount_krw, pos)
            if amt > 0:
                orders.append({"kind": "BUY_UP", "ticker": pos.ticker, "amount_krw": amt,
                               "price": price, "reason": act.reason, "sleeve": pos.sleeve,
                               "pre_state": pre})
                notes.append(f"{pos.ticker} 불타기 {amt:,.0f} KRW — {act.reason}")
            return
        if act.kind == "BUY_DOWN":
            amt = self._cap_buy(act.amount_krw, pos)
            if amt > 0:
                orders.append({"kind": "BUY_DOWN", "ticker": pos.ticker, "amount_krw": amt,
                               "price": price, "reason": act.reason, "sleeve": pos.sleeve,
                               "pre_state": pre})
                notes.append(f"{pos.ticker} 물타기 {amt:,.0f} KRW — {act.reason}")
            return

        # 2) 전략 엑시트 (DC20 이탈 / ATR 손절)
        ex = strat.check_exit(candles, pos.entry_price, pos.atr, live_price=price)
        if ex.signal == "EXIT":
            orders.append({"kind": "SELL_ALL", "ticker": pos.ticker, "ratio": 1.0,
                           "price": price, "reason": ex.reason, "sleeve": pos.sleeve,
                           "stop": True, "pre_state": pre})
            notes.append(f"{pos.ticker} 전략 청산 — {ex.reason}")
            return

        # 3) 5M → 1D 승급
        if pos.sleeve == "5M" and c5 is not None:
            atr5 = float(pos.atr)
            if self.risk.approve_graduation(pos.ticker, price, atr5):
                sig1d = self.turtle_1d.check_entry(c1d)      # 완료 일봉 종가 기준
                if sig1d.signal == "LONG":
                    amt = calc_position_size(self.equity_krw, sig1d.atr, price, SLEEVE_1D["risk_pct"])
                    amt = self._cap_buy(amt, pos)
                    if amt > 0:
                        orders.append({"kind": "GRADUATE", "ticker": pos.ticker,
                                       "amount_krw": amt, "price": price, "atr": sig1d.atr,
                                       "qty": amt * (1 - FEE) / price,
                                       "reason": f"5M→1D 승급 (peak {pos.peak_gain_pct(price):+.2f}%)",
                                       "sleeve": "1D", "from_sleeve": "5M", "pre_state": pre})
                        notes.append(f"{pos.ticker} 5M→1D 승급 {amt:,.0f} KRW")
                        return

        notes.append(f"{pos.ticker} HOLD — {act.reason}")

    def _cap_buy(self, amount_krw: float, pos: Optional[SleevePosition] = None) -> float:
        """증액(불타기/물타기/승급) 주문 상한 — 주문·종목·슬리브·포트폴리오·현금·종목리스크."""
        if amount_krw < MIN_ORDER_KRW:
            return 0.0
        sleeve = pos.sleeve if pos else "1D"
        eq = max(self.equity_krw, 1.0)
        rooms = [
            eq * ORDER_CAP[sleeve],
            eq * SYMBOL_CAP[sleeve] - (pos.amount_krw if pos else 0.0),
            eq * SLEEVE_NOTIONAL_CAP[sleeve] - self._sleeve_exposure(sleeve),
            eq * PORTFOLIO_NOTIONAL_CAP - self._portfolio_exposure(),
        ]
        if self._cycle_cash is not None:
            rooms.append(self._cycle_cash * 0.995)
        if pos is not None and pos.atr > 0 and pos.avg_price > 0:
            held = pos.extras.get("qty", 0.0)
            max_qty = SYMBOL_RISK_CAP * eq / (2.0 * pos.atr)
            rooms.append(max(max_qty - held, 0.0) * pos.avg_price)
        room = min(rooms)
        if room < MIN_ORDER_KRW:
            return 0.0
        return float(int(min(amount_krw, room)))

    def _open(self, ticker: str, sleeve: str, sig, price: float,
              orders: List[dict], notes: List[str]) -> None:
        cfg = SLEEVE_1D if sleeve == "1D" else SLEEVE_5M
        if price <= 0 or sig.atr <= 0:
            notes.append(f"{ticker} {sleeve} 진입 보류 — 가격/ATR 무효")
            return
        unit = calc_position_size(self.equity_krw, sig.atr, price, cfg["risk_pct"])
        if unit <= 0:
            notes.append(f"{ticker} {sleeve} 진입 보류 — 유닛 계산 실패")
            return

        # 변동성이 하한보다 낮으면 ATR 사이징이 자본 전액을 요구한다 → 주문 캡으로 제한
        eq = max(self.equity_krw, 1.0)
        vol_ratio = sig.atr / price
        caps = [
            eq * ORDER_CAP[sleeve],                                  # 주문 1건 상한
            eq * SYMBOL_CAP[sleeve] - self._symbol_exposure(ticker),  # 종목 총노출
            eq * SLEEVE_NOTIONAL_CAP[sleeve] - self._sleeve_exposure(sleeve),
            eq * PORTFOLIO_NOTIONAL_CAP - self._portfolio_exposure(),
        ]
        if vol_ratio < MIN_VOL_FLOOR[sleeve]:
            notes.append(f"{ticker} {sleeve} 변동성 {vol_ratio:.3%} < 하한 {MIN_VOL_FLOOR[sleeve]:.2%} — 주문캡 적용")
        if self._cycle_cash is not None:
            caps.append(self._cycle_cash * 0.995)                     # 가용 원화
        unit = float(min([unit] + [c for c in caps if c is not None]))
        if unit < MIN_ORDER_KRW:
            notes.append(f"{ticker} {sleeve} 진입 거부 — 상한 적용 후 {unit:,.0f} KRW < 최소주문")
            return

        approved, reason = self.risk.approve_entry(ticker, sleeve, unit, price, sig.atr)
        if approved < MIN_ORDER_KRW:
            notes.append(f"{ticker} {sleeve} 진입 거부 — {reason}")
            return

        qty = approved * (1 - FEE) / price        # 수수료 반영 수량 (감사 LIFECYCLE-01)
        risk_pct = (2.0 * float(sig.atr) * qty) / eq
        if risk_pct > SYMBOL_RISK_CAP:            # 종목 리스크 하드 캡
            qty = SYMBOL_RISK_CAP * eq / (2.0 * float(sig.atr))
            approved = qty * price / (1 - FEE)
            risk_pct = SYMBOL_RISK_CAP
            if approved < MIN_ORDER_KRW:
                notes.append(f"{ticker} {sleeve} 진입 거부 — 종목 리스크 캡 초과")
                return

        if self._cycle_cash is not None:
            self._cycle_cash -= approved

        # ⚠️ 포지션 등록은 사이클 끝(주문 한도 확정 후) _commit_entry 에서 한다.
        orders.append({"kind": "BUY", "ticker": ticker, "sleeve": sleeve,
                       "amount_krw": float(int(approved)), "price": price,
                       "atr": float(sig.atr), "strategy": f"turtle_{sleeve.lower()}",
                       "reason": f"{sig.reason} | {reason}", "qty": qty, "risk_pct": risk_pct})
        notes.append(f"{ticker} {sleeve} 신규 진입 {approved:,.0f} KRW — {sig.reason}")

    def _commit_entry(self, order: dict) -> None:
        """주문이 실제로 나갈 때만 포지션을 등록한다 (유령 포지션 방지 — 감사 STATE-06)."""
        ticker, sleeve = order["ticker"], order["sleeve"]
        cfg = SLEEVE_1D if sleeve == "1D" else SLEEVE_5M
        pos = SleevePosition(
            ticker=ticker, sleeve=sleeve, strategy=order.get("strategy", f"turtle_{sleeve.lower()}"),
            entry_price=float(order["price"]), atr=float(order["atr"]),
            amount_krw=float(order["amount_krw"]), units=1, unit_krw=float(order["amount_krw"]),
            last_buy_price=float(order["price"]), highest_price=float(order["price"]),
            risk_pct=float(order.get("risk_pct", 0.0)), max_pyramid=int(cfg["max_pyramid"]),
            opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        pos.extras["qty"] = float(order.get("qty") or 0.0)
        self.risk.register(pos, qty=pos.extras["qty"])
        if sleeve == "5M":
            self.limiter.record_entry(ticker)
        order["committed"] = True

    # ---------------- 체결 반영/롤백 ----------------
    def on_order_filled(self, order: dict, fill_price: Optional[float] = None) -> None:
        price = float(fill_price or order.get("price") or 0)
        ticker = order["ticker"]
        kind = order["kind"]
        if kind == "BUY":
            qty = float(order.get("qty") or (float(order["amount_krw"]) * (1 - FEE) / price if price > 0 else 0.0))
            self._daily_trades_count += 1
            self._journal("buy", ticker, qty, price, float(order["amount_krw"]), 0.0,
                          "BUY", order.get("sleeve", ""), order.get("reason", ""))
        elif kind in ("BUY_UP", "BUY_DOWN"):
            qty = float(order["amount_krw"]) / price if price > 0 else 0.0
            self.risk.on_buy(ticker, price, float(order["amount_krw"]), kind=kind)
            self._daily_trades_count += 1
            self._journal("buy", ticker, qty, price, float(order["amount_krw"]), 0.0,
                          kind, order.get("sleeve", ""), order.get("reason", ""))
        elif kind == "SELL_PARTIAL":
            pos = self.risk.get(ticker)
            qty = (pos.extras.get("qty", 0.0) if pos else 0.0) * float(order["ratio"])
            avg = pos.avg_price if pos else price
            self.risk.on_sell(ticker, price, float(order["ratio"]))
            self._daily_trades_count += 1
            self._journal("sell", ticker, qty, price, qty * price, (price - avg) * qty,
                          kind, order.get("sleeve", ""), order.get("reason", ""))
        elif kind == "SELL_ALL":
            pos = self.risk.get(ticker)
            qty = pos.extras.get("qty", 0.0) if pos else 0.0
            avg = pos.avg_price if pos else price
            if pos:
                self.risk.on_sell(ticker, price, 1.0)
                if order.get("stop"):
                    if pos.sleeve == "5M":
                        self.limiter.record_stop(ticker)
                    self.prot.record_stop(self.now_fn())
            self._daily_trades_count += 1
            self._journal("sell", ticker, qty, price, qty * price, (price - avg) * qty,
                          kind, order.get("sleeve", ""), order.get("reason", ""))
        elif kind == "GRADUATE":
            # 5M 전량 청산 + 1D 재등록
            old = self.risk.remove(ticker)
            real_pnl = 0.0
            old_qty = 0.0
            if old:
                old_qty = old.extras.get("qty", 0.0)
                real_pnl = old.realized_krw + (price - old.avg_price) * old_qty
            new = SleevePosition(
                ticker=ticker, sleeve="1D", strategy="turtle_1d", entry_price=price,
                atr=float(order.get("atr") or 0) or price * 0.03,
                amount_krw=float(order["amount_krw"]), units=1,
                unit_krw=float(order["amount_krw"]), last_buy_price=price, highest_price=price,
                risk_pct=(2 * (float(order.get("atr") or price * 0.03) / price)
                          * (float(order["amount_krw"]) / max(self.equity_krw, 1.0))),
                max_pyramid=2, realized_krw=real_pnl,
                opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            new.extras["qty"] = float(order.get("qty") or
                                      (float(order["amount_krw"]) * (1 - FEE) / price if price > 0 else 0.0))
            self.risk.register(new)
            self._daily_trades_count += 2
            self._journal("sell", ticker, old_qty, price, old_qty * price,
                          real_pnl, "GRADUATE_SELL_5M", "5M", order.get("reason", ""))
            self._journal("buy", ticker, new.extras["qty"], price, float(order["amount_krw"]), 0.0,
                          "GRADUATE_BUY_1D", "1D", order.get("reason", ""))
        self.persist()

    # ---------------- 거래 원장 ----------------
    def _journal(self, side: str, ticker: str, qty: float, price: float, notional: float,
                 realized_pnl: float, kind: str, sleeve: str, reason: str) -> None:
        """append-only 체결 원장 — 세금/성과분석/대사의 단일 출처."""
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "paper" if self.paper else "live",
            "ticker": ticker, "side": side, "qty": round(qty, 12), "price": price,
            "notional": round(notional, 2), "fee": round(abs(notional) * FEE, 2),
            "realized_pnl": round(realized_pnl, 2), "kind": kind, "sleeve": sleeve,
            "reason": reason,
        }
        try:
            paths.ensure_dirs()
            with open(paths.TRADES_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:                     # noqa: BLE001
            log.warning("거래 원장 기록 실패: %s", e)

    def rollback_order(self, order: dict, reason: str = "주문 실패") -> None:
        """주문 실패 시 낙관적으로 반영했던 상태를 되돌린다.

        BUY  : 포지션 자체를 제거
        그 외: 주문 생성 시점의 스냅샷(pre_state)으로 복원 (GRADUATE 포함)
        일일 거래 카운터는 '체결'에서만 증가하므로 여기서 건드리지 않는다.
        """
        ticker, kind = order["ticker"], order["kind"]
        try:
            if kind == "BUY":
                self.risk.remove(ticker)
            elif "pre_state" in order:
                self._restore_state(ticker, order["pre_state"], kind)
            elif kind in ("BUY_UP", "BUY_DOWN"):
                # 스냅샷이 없는 주문(구버전/수동)은 역산으로 되돌린다
                pos = self.risk.get(ticker)
                if pos is not None:
                    amt = float(order.get("amount_krw", 0))
                    px = float(order.get("price") or 0)
                    if px > 0:
                        pos.extras["qty"] = max(pos.extras.get("qty", 0.0) - amt / px, 0.0)
                    pos.amount_krw = max(pos.amount_krw - amt, 0.0)
                    pos.units = max(pos.units - 1, 1)
                    if kind == "BUY_UP":
                        pos.fire_count = max(pos.fire_count - 1, 0)
                    else:
                        pos.stage = max(pos.stage - 1, 1)
            log.warning("주문 롤백 %s %s — %s", ticker, kind, reason)
        finally:
            self.persist()

    @staticmethod
    def _pos_state(pos: SleevePosition) -> dict:
        """주문 직전 포지션 스냅샷 (롤백/감사용)."""
        return {
            "sleeve": pos.sleeve, "strategy": pos.strategy, "entry_price": pos.entry_price,
            "atr": pos.atr, "amount_krw": pos.amount_krw, "units": pos.units,
            "unit_krw": pos.unit_krw, "last_buy_price": pos.last_buy_price,
            "highest_price": pos.highest_price, "risk_pct": pos.risk_pct,
            "max_pyramid": pos.max_pyramid, "fire_count": pos.fire_count, "stage": pos.stage,
            "ctp_active": pos.ctp_active,
            "ctp_ladder": [list(x) for x in pos.ctp_ladder],
            "ctp_ladder_peak": pos.ctp_ladder_peak, "ctp_ladder_gen": pos.ctp_ladder_gen,
            "exit_stage": pos.exit_stage, "realized_krw": pos.realized_krw,
            "opened_at": pos.opened_at, "extras": dict(pos.extras),
        }

    def _restore_state(self, ticker: str, state: dict, kind: str) -> None:
        pos = self.risk.get(ticker)
        if pos is None:                      # SELL 로 삭제된 경우 재생성
            data = {k: v for k, v in state.items()}
            pos = SleevePosition(ticker=ticker, **data)
            self.risk.register(pos, qty=pos.extras.get("qty", 0.0))
            return
        for k, v in state.items():
            if k == "extras":
                pos.extras = dict(v)
            elif hasattr(pos, k):
                setattr(pos, k, v)

    # ---------------- 스냅샷 ----------------
    def _snapshot_positions(self, live_prices: Dict[str, float]) -> List[dict]:
        out = []
        for t, p in self.risk.positions.items():
            price = live_prices.get(t)
            if not price:
                for tf in ("5m", "1h", "1d"):
                    c = self._load(tf, t, limit=1)
                    if c is not None and len(c):
                        price = c.close[-1]
                        break
            price = float(price or p.entry_price)
            qty = p.extras.get("qty", 0.0)
            out.append({
                "ticker": t, "sleeve": p.sleeve, "strategy": p.strategy,
                "avg_price": round(p.avg_price, 6), "qty": round(qty, 8),
                "price": round(price, 6),
                "value_krw": round(qty * price),
                "pnl_pct": round((price / p.avg_price - 1) * 100, 2) if p.avg_price else 0.0,
                "peak_pct": round(p.peak_gain_pct(p.highest_price), 2),
                "ctp": "ON" if p.ctp_active else "OFF",
                "units": p.units,
            })
        return out

    def status_lines(self) -> List[str]:
        eq = self.equity_krw
        lines = [f"자본 {eq:,.0f} KRW / 포지션 {len(self.risk.positions)}종 "
                 f"(리스크 {self.risk.total_risk():.2%} / 캡 {PORTFOLIO_TOTAL_CAP:.0%})",
                 f"일일 거래 {self._daily_trades_count}/{self.max_daily_trades}회 "
                 f"· 기준자본 {self._daily_starting_equity:,.0f}"]
        for p in self._snapshot_positions({}):
            lines.append(f"  {p['ticker']} [{p['sleeve']}] {p['qty']:.6f}개 평단 {p['avg_price']:,.4f} "
                         f"→ {p['price']:,.4f} ({p['pnl_pct']:+.2f}%) peak {p['peak_pct']:+.2f}% CTP {p['ctp']}")
        if paths.emergency_active():
            lines.append("🚨 EMERGENCY STOP 파일 존재")
        return lines


if __name__ == "__main__":
    """드라이런: DB만으로 1사이클 실행 (주문 실행 없음)."""
    import sys
    from config import Config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load()
    reg = MTFEngine(cfg.tickers, cfg.db_path, equity_krw=cfg.initial_equity_krw,
                    max_daily_loss_pct=cfg.max_daily_loss_pct, max_daily_trades=cfg.max_daily_trades,
                    start_equity_krw=cfg.start_equity_krw)
    # 실시간 피드가 없는 드라이런에서는 종가를 가격으로 사용
    closes = {}
    for t in cfg.tickers:
        c = datastore.load(cfg.db_path, "1d", t, limit=1)
        if len(c):
            closes[t] = float(c.close[-1])
    res = reg.run_cycle(live_prices=closes)
    print(f"\n=== 사이클 {res['timestamp']} ===")
    print("레짐:", ", ".join(f"{k.replace('KRW-', '')}={v}" for k, v in res["regimes"].items()))
    print(f"주문 {len(res['orders'])}건")
    for o in res["orders"]:
        print("  ", o)
    for s in res["summary"]:
        print("  -", s)
    print()
    for line in reg.status_lines():
        print(line)
