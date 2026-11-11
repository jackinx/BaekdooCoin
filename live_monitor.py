# -*- coding: utf-8 -*-
"""라이브 모니터 (원본 live_monitor.py) — Windows 포팅.

실행:
  python live_monitor.py                  # 감시만 (주문 없음) ← 먼저 이걸로 며칠 관찰
  python live_monitor.py --trade          # 실제 주문 (⚠️ 실제 자금)
  python live_monitor.py --once           # 1사이클만 실행하고 종료 (점검용)
  python live_monitor.py --no-ws          # WebSocket 없이 DB 종가만으로 사이클

스레드:
  ws        업비트 WebSocket → 실시간 가격 캐시
  updater   5분마다 data_updater.py (증분 갱신)
  telegram  /status /stop /resume ... (chat_id 화이트리스트)
  main      1분마다 engine.run_cycle() → 주문 실행
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional

import requests

import datastore
import paths
from config import Config
from engine_mtf import MTFEngine
from telegram_control import HELP_TEXT, TelegramController
from upbit_client import UpbitClient, UpbitError

log = logging.getLogger("baekdoocoin")


# ---------------- 로깅 ----------------
def setup_logging(verbose: bool = False) -> None:
    paths.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(paths.LOG_DIR / "live.log", maxBytes=8_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


# ---------------- PID 락 ----------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    except Exception:                                # noqa: BLE001
        return False


class PidLock:
    def __init__(self, path: Path):
        self.path = path

    def acquire(self) -> bool:
        """O_CREAT|O_EXCL 로 원자적 배타 생성 (check-then-write 는 경쟁 조건이 있다)."""
        paths.ensure_dirs()
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (ValueError, OSError):
                    pid = 0
                if _pid_alive(pid):
                    log.error("이미 실행 중 (PID %d). 중복 실행 방지.", pid)
                    return False
                log.warning("죽은 PID %s 락 정리", pid)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        return False

    def release(self) -> None:
        try:
            if self.path.exists() and self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except Exception:                            # noqa: BLE001
            pass


# ---------------- 라이브 모니터 ----------------
PRICE_MAX_AGE_S = 120          # WS 가격 신선도 상한 (초)


class LiveMonitor:
    def __init__(self, cfg: Config, trade: bool = False, use_ws: bool = True,
                 paper: Optional[bool] = None):
        self.cfg = cfg
        self.trade = trade
        self.use_ws = use_ws
        self.paper = (not trade) if paper is None else bool(paper)
        self.db = cfg.db_path
        self._prices: Dict[str, dict] = {}
        self._price_lock = threading.Lock()
        self._equity_fail = 0
        self._order_fail: Dict[str, int] = {}

        self.client: Optional[UpbitClient] = None
        if cfg.has_upbit():
            try:
                self.client = UpbitClient(cfg.upbit_access_key, cfg.upbit_secret_key)
            except UpbitError as e:
                log.error("업비트 클라이언트 생성 실패: %s", e)

        equity = cfg.initial_equity_krw
        if self.client:
            try:
                eq = self.client.get_total_equity_krw()
                if eq > 0:
                    equity = eq
            except Exception as e:                   # noqa: BLE001
                log.warning("총 평가액 조회 실패(기본 자본 사용): %s", e)

        self.engine = MTFEngine(
            tickers=cfg.tickers, db_path=self.db, equity_krw=equity, client=self.client,
            max_daily_loss_pct=cfg.max_daily_loss_pct, max_daily_trades=cfg.max_daily_trades,
            max_orders_per_cycle=cfg.max_orders_per_cycle, paper=self.paper,
            start_equity_krw=cfg.start_equity_krw,
        )
        if self.client:
            self.engine.sync_from_exchange()

        self._public = requests.Session()
        self.tg: Optional[TelegramController] = None
        self._stop = threading.Event()
        self._cycle_no = 0

    def _public_price(self, ticker: str) -> Optional[float]:
        """API 키 없이 공개 시세 조회 — 키 발급 전 '관찰 모드'용."""
        try:
            r = self._public.get("https://api.upbit.com/v1/ticker", params={"markets": ticker},
                                 timeout=8, headers={"User-Agent": "baekdoocoin-win/1.0"})
            r.raise_for_status()
            return float(r.json()[0]["trade_price"])
        except Exception as e:                            # noqa: BLE001
            log.debug("공개 시세 조회 실패 %s: %s", ticker, e)
            return None

    # ---------------- 자본(equity) ----------------
    def _refresh_equity(self) -> bool:
        """사이클마다 총 평가액을 갱신한다 — 이게 없으면 일일손실 킬스위치가 죽는다(감사 F01)."""
        if not self.client:
            return False
        try:
            eq = float(self.client.get_total_equity_krw())
        except Exception as e:                       # noqa: BLE001
            eq = 0.0
            log.warning("총 평가액 조회 실패: %s", e)
        if eq <= 0:
            self._equity_fail += 1
            self.engine.external_block = (
                f"자본 조회 실패 {self._equity_fail}회 — 신규 진입 차단" if self._equity_fail >= 3 else None)
            return False
        self._equity_fail = 0
        self.engine.external_block = None
        self.engine.equity_krw = eq
        self.engine.risk.equity_krw = eq
        return True

    # ---------------- 가격 ----------------
    def live_prices(self, max_age_s: int = PRICE_MAX_AGE_S) -> Dict[str, float]:
        """신선한 가격만 반환 — 스테일 가격으로 주문하지 않는다(감사 F10)."""
        now = datetime.now(timezone.utc)
        out: Dict[str, float] = {}
        with self._price_lock:
            for t, v in self._prices.items():
                px = float(v.get("price") or 0)
                ts = v.get("ts")
                if px <= 0 or not ts:
                    continue
                try:
                    age = (now - datetime.fromisoformat(ts)).total_seconds()
                except ValueError:
                    continue
                if age <= max_age_s:
                    out[t] = px
                else:
                    log.debug("%s 가격 스테일(%.0fs) — 제외", t, age)
        return out

    def _ws_loop(self) -> None:
        import websocket                                  # websocket-client
        url = "wss://api.upbit.com/websocket/v1"
        sub = [{"ticket": "baekdoocoin-win"}, {"type": "ticker", "codes": self.cfg.tickers,
                                               "is_only_realtime": True}]
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(url, timeout=20)
                ws.send(json.dumps(sub))
                log.info("WebSocket 연결 — %d종 구독", len(self.cfg.tickers))
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        break
                    d = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                    code, price = d.get("code"), d.get("trade_price")
                    if code and price:
                        with self._price_lock:
                            self._prices[code] = {"price": float(price),
                                                  "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                ws.close()
            except Exception as e:                        # noqa: BLE001
                log.warning("WebSocket 오류: %s — 5초 후 재연결", e)
                time.sleep(5)

    def _updater_loop(self, every: int = 300) -> None:
        from data_updater import update
        while not self._stop.is_set():
            try:
                n = update(self.db, self.cfg.tickers, ["1d", "4h", "1h"])
                if n:
                    log.info("데이터 갱신 %d행", n)
            except Exception as e:                        # noqa: BLE001
                log.warning("데이터 갱신 실패: %s", e)
            self._stop.wait(every)

    # ---------------- 주문 실행 ----------------
    def _exchange_qty(self, ticker: str) -> Optional[float]:
        """거래소 실보유 수량 (조회 실패면 None)."""
        if not self.client:
            return None
        try:
            bal = self.client.get_coin_balance(ticker)
            return float(bal[0]) if isinstance(bal, tuple) else float(bal)
        except Exception as e:                            # noqa: BLE001
            log.warning("잔고 조회 실패 %s: %s", ticker, e)
            return None

    def _execute(self, order: dict) -> bool:
        ticker, kind = order["ticker"], order["kind"]
        price = float(order.get("price") or 0)
        if not self.trade or self.client is None:
            log.info("[PAPER] %s %s %s", kind, ticker, order.get("reason", ""))
            self._order_fail[ticker] = 0
            return True

        try:
            if kind in ("BUY", "BUY_UP", "BUY_DOWN", "GRADUATE"):
                amt = float(order["amount_krw"])
                if kind == "GRADUATE":
                    pos = self.engine.risk.get(ticker)
                    if pos and pos.extras.get("qty", 0) > 0:
                        self.client.market_sell(ticker, pos.extras["qty"])
                        log.info("승급 매도 %s %.8f개", ticker, pos.extras["qty"])
                self.client.market_buy(ticker, amt)
                log.info("✅ 매수 %s %s KRW (%s)", ticker, f"{amt:,.0f}", order.get("reason", ""))
            elif kind in ("SELL_ALL", "SELL_PARTIAL"):
                pos = self.engine.risk.get(ticker)
                if not pos:
                    log.warning("매도 대상 포지션 없음: %s", ticker)
                    return False
                qty = pos.extras.get("qty", 0.0) * float(order.get("ratio", 1.0))
                # 거래소 실잔고로 클램프 — 추적 수량이 부풀어 있으면 매도가 거부된다(감사 LIFECYCLE-01)
                live_qty = self._exchange_qty(ticker)
                if live_qty is not None:
                    if live_qty <= 0:
                        log.warning("%s 거래소 잔고 0 — 매도 생략", ticker)
                        return False
                    qty = min(qty, live_qty)
                if qty <= 0:
                    return False
                self.client.market_sell(ticker, qty)
                log.info("✅ 매도 %s %.8f개 (%s)", ticker, qty, order.get("reason", ""))
            else:
                log.warning("알 수 없는 주문 종류: %s", kind)
                return False
            self._order_fail[ticker] = 0
            return True
        except Exception as e:                            # noqa: BLE001
            n = self._order_fail.get(ticker, 0) + 1
            self._order_fail[ticker] = n
            log.error("❌ 주문 실패 %s %s (%d회): %s", kind, ticker, n, e)
            if n >= 2:                                   # 반복 실패는 조용히 넘기지 않는다
                msg = f"🚨 {ticker} 주문 {n}회 연속 실패 — {kind}: {e}"
                log.error(msg)
                if self.tg:
                    self.tg.send(msg)
                if kind in ("SELL_ALL", "SELL_PARTIAL") and n >= 3:
                    paths.emergency_stop()
                    if self.tg:
                        self.tg.send("🚨 청산 반복 실패 → 킬스위치 생성(수동 확인 필요)")
            return False

    # ---------------- 상태 응답 ----------------
    def _tg_status(self) -> str:
        lines = ["📊 백두코인봇 상태",
                 f"모드: {'🔴 실거래' if self.trade else '🟡 감시(주문없음)'} · "
                 f"WS: {'ON' if self.use_ws else 'OFF'} · 사이클 {self._cycle_no}회"]
        if paths.emergency_active():
            lines.append("🚨 킬스위치 ON")
        if paths.paused():
            lines.append("⏸️ 일시정지 상태")
        if self.engine.state_error:
            lines.append(f"⚠️ 상태파일 오류: {self.engine.state_error}")
        if self.engine.external_block:
            lines.append(f"⚠️ {self.engine.external_block}")
        lines += self.engine.status_lines()
        lp = self.live_prices()
        if lp:
            lines.append("실시간가: " + ", ".join(f"{k.replace('KRW-', '')}={v:,.4f}" for k, v in list(lp.items())[:6]))
        return "\n".join(lines)

    def _tg_balance(self) -> str:
        if not self.client:
            return "업비트 키 미설정 — .env 확인"
        krw = self.client.get_krw_balance()
        total = self.client.get_total_equity_krw()
        out = [f"💰 원화 {krw:,.0f} KRW / 총평가 {total:,.0f} KRW"]
        for t, i in self.client.get_all_balances().items():
            out.append(f"  {t}: {i['qty']:.8f}개 평단 {i['avg_price']:,.4f} 평가 {i['value_krw']:,.0f}")
        return "\n".join(out)

    def _tg_positions(self) -> str:
        snaps = self.engine._snapshot_positions(self.live_prices())
        if not snaps:
            return "보유 포지션 없음"
        return "\n".join(
            f"{s['ticker']} [{s['sleeve']}] {s['qty']:.6f}개 평단 {s['avg_price']:,.4f} "
            f"현재 {s['price']:,.4f} ({s['pnl_pct']:+.2f}%) peak {s['peak_pct']:+.2f}% CTP {s['ctp']}"
            for s in snaps)

    def _tg_regime(self) -> str:
        from strategies.regime_detector import RegimeDetector
        det = RegimeDetector()
        out = ["🌡️ 레짐 (1H, DC55 override)"]
        for t in self.cfg.tickers:
            c = datastore.load(self.db, "1h", t, limit=600)
            if len(c) < 60:
                out.append(f"  {t}: 데이터 없음")
                continue
            r, x = det.detect_regime(c, dc_high_override=True)
            out.append(f"  {t.replace('KRW-', '')}: {r.value} ADX={x['adx']:.0f} RSI={x['rsi']:.0f}")
        return "\n".join(out)

    def _tg_data(self) -> str:
        rows = datastore.stats(self.db)
        if not rows:
            return "DB 비어 있음 — data_crawler.py 또는 tools/import_legacy_db.py 실행"
        lines = ["🗄️ DB 통계"]
        for r in rows:
            if r["ticker"] in self.cfg.tickers:
                lines.append(f"  {r['timeframe']:>3} {r['ticker']:<10} {r['rows']:>6}행 ~{r['to'][:10]}")
        return "\n".join(lines)

    def _setup_telegram(self) -> None:
        if not self.cfg.has_telegram():
            log.info("텔레그램 미설정 — 원격제어 생략")
            return
        handlers = {
            "start": lambda: HELP_TEXT,
            "help": lambda: HELP_TEXT,
            "_help_text": lambda: HELP_TEXT,
            "status": self._tg_status,
            "balance": self._tg_balance,
            "positions": self._tg_positions,
            "regime": self._tg_regime,
            "data": self._tg_data,
            "stop": self._tg_stop,
            "resume": self._tg_resume,
            "pause": self._tg_pause,
        }
        self.tg = TelegramController(self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, handlers)
        self.tg.start_polling()
        self.tg.send("🤖 백두코인봇(Windows) 시작 — "
                     f"{'🔴 실거래' if self.trade else '🟡 감시 모드'} / {len(self.cfg.tickers)}종\n/help 로 명령 확인")

    def _tg_stop(self) -> str:
        self.engine.stop("telegram /stop")
        paths.emergency_stop()
        return "🚨 비상정지 ON — 모든 매매 중단 (kill-switch 파일 생성)"

    def _tg_resume(self) -> str:
        self.engine.resume()
        return "▶️ 매매 재개 (kill-switch 제거)"

    def _tg_pause(self) -> str:
        paths.pause()                      # 파일로 남겨 재시작 후에도 유지된다
        return "⏸️ 일시정지 (신규 진입 중단, 청산은 계속) — /resume 로 재개"

    # ---------------- 메인 ----------------
    def _notify(self, msg: str) -> None:
        log.info(msg)
        if self.tg:
            self.tg.send(msg)

    def run_once(self) -> dict:
        self._cycle_no += 1
        self._refresh_equity()                                 # 감사 F01
        prices = self.live_prices() if self.use_ws else {}
        for t in self.cfg.tickers:                             # 없는 종목만 REST 폴백
            if t in prices:
                continue
            if self.client:
                try:
                    prices[t] = self.client.get_current_price(t)
                    continue
                except Exception:                              # noqa: BLE001
                    pass
            px = self._public_price(t)                         # 키 없이도 공개 시세 사용
            if px:
                prices[t] = px
        # 가격을 확보하지 못한 종목은 주문 금지 (스테일/블라인드 트레이딩 방지 — 감사 F10)
        blocked = {t for t in self.cfg.tickers if t not in prices}
        cash = None
        if self.client:
            try:
                cash = float(self.client.get_krw_balance())
            except Exception as e:                             # noqa: BLE001
                log.warning("원화 잔고 조회 실패: %s", e)
        result = self.engine.run_cycle(live_prices=prices, cash_krw=cash, blocked_tickers=blocked)
        if result.get("emergency_stop"):
            self._notify(f"🚨 비상정지: {result['emergency_stop']} (청산 주문은 계속 실행)")
        if blocked and self._cycle_no % 10 == 0:
            self._notify(f"⚠️ 가격 미확보 종목 {len(blocked)}개 — 주문 금지: "
                         + ", ".join(sorted(x.replace('KRW-', '') for x in blocked)))
        if len(result["orders"]) >= 8:
            self._notify(f"⚠️ 사이클 주문 {len(result['orders'])}건 — 한도({self.engine.max_orders_per_cycle}) 근접")

        for order in result["orders"]:
            ok = self._execute(order)
            if ok:
                self.engine.on_order_filled(order, fill_price=prices.get(order["ticker"], order.get("price")))
                self._notify(f"{'✅' if self.trade else '📝(paper)'} {order['kind']} {order['ticker']} — {order.get('reason', '')}")
            else:
                self.engine.rollback_order(order, "주문 실패")
                if order.get("kind") == "BUY":
                    result["orders_failed"] = result.get("orders_failed", 0) + 1
        if result["orders"]:
            self._notify(f"사이클 {self._cycle_no}: 주문 {len(result['orders'])}건 / "
                         f"포지션 {len(result['positions'])}종 / 자본 {result['equity_krw']:,.0f}")
        return result

    def run(self, interval: int = 60) -> None:
        threads = []
        if self.use_ws:
            t = threading.Thread(target=self._ws_loop, name="baekdoocoin-ws", daemon=True)
            t.start()
            threads.append(t)
        t = threading.Thread(target=self._updater_loop, name="baekdoocoin-updater", daemon=True)
        t.start()
        threads.append(t)

        self._setup_telegram()
        log.info("=== 백두코인봇 가동 === %s / %d종 / %d초 사이클 / DB=%s",
                 "🔴 실거래" if self.trade else "🟡 감시", len(self.cfg.tickers), interval, self.db)

        while not self._stop.is_set():
            started = time.time()
            try:
                self.run_once()
            except Exception as e:                         # noqa: BLE001
                log.exception("사이클 오류: %s", e)
                if self.tg:
                    self.tg.send(f"⚠️ 사이클 오류: {e}")
            elapsed = time.time() - started
            self._stop.wait(max(1.0, interval - elapsed))

    def shutdown(self, *args) -> None:
        log.warning("종료 신호 — 그레이스풀 셧다운")
        self.engine.persist()
        self._stop.set()
        if self.tg:
            try:
                self.tg.send("🛑 백두코인봇 종료 (포지션 상태 저장됨)")
            except Exception:                              # noqa: BLE001
                pass
            self.tg.stop_polling()


def main() -> int:
    ap = argparse.ArgumentParser(description="백두코인봇 (Windows) 라이브 모니터")
    ap.add_argument("--trade", action="store_true", help="실제 주문 실행 (기본=감시만)")
    ap.add_argument("--once", action="store_true", help="1사이클만 실행 후 종료")
    ap.add_argument("--no-ws", action="store_true", help="WebSocket 비활성 (DB 종가 사용)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--cycle", type=int, default=None, help="사이클 주기(초)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = Config.load()
    if args.db:
        cfg.db_path = args.db
    interval = args.cycle or cfg.cycle_seconds

    datastore.init_db(cfg.db_path)
    if cfg.missing_required() and args.trade:
        log.error("실거래에는 업비트 API 키가 필요합니다: %s", ", ".join(cfg.missing_required()))
        return 2

    lock = PidLock(paths.RUNTIME_DIR / "live_monitor.pid")
    if not lock.acquire():
        return 3
    try:
        mon = LiveMonitor(cfg, trade=args.trade, use_ws=not args.no_ws)
        signal.signal(signal.SIGINT, mon.shutdown)
        signal.signal(signal.SIGTERM, mon.shutdown)
        if args.once:
            res = mon.run_once()
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        mon.run(interval)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
