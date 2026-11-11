"""
백두코인봇 라이브 모니터 — 실시간 시세 감시 + 자동매매 (RiskManager v2 통합)
====================================================================

업비트 WebSocket을 통해 실시간 가격을 수신하고,
백두코인봇Engine을 1분마다 실행하여 신호를 생성 및 주문 실행.

RiskManager v2가 엔진 내에 내장되어 있으므로 별도 전략 로직 불필요.

사용법:
    python live_monitor.py           # 감시만 (주문 안 함)
    python live_monitor.py --trade   # 감시 + 자동매매

동작 방식:
    1. WebSocket으로 10종 실시간 체결가 수신 (_price_cache)
    2. 1분마다 engine.run_cycle() 실행 (레짐 감지 → 전략 선택 → 신호 → 주문 생성)
    3. 생성된 주문을 시장가 매수/매도 (KRW 금액 기준)
    4. 비상정지(/stop)는 텔레그램으로 제어
"""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LiveMonitor:
    """실시간 시세 감시 + 엔진 기반 자동매매.

    업비트 WebSocket + 엔진 기반 주문 생성.
    """

    # 감시할 티커 (메이저 10종)
    TICKERS = [
        "KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA",
        "KRW-DOGE", "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK",
    ]

    def __init__(
        self,
        config,
        db_path: str = "data/upbit_v2.db",
        *,
        trading_enabled: bool = False,
    ):
        """
        Parameters
        ----------
        config : Config
            config.py에서 로드한 설정.
        db_path : str
            SQLite DB 경로.
        trading_enabled : bool
            True면 실제 주문 실행.
        """
        self.config = config
        self.db_path = db_path
        self.trading_enabled = trading_enabled

        # 실시간 가격 캐시: {ticker: {"price": float, "timestamp": float}}
        self._price_cache: dict[str, dict] = {}
        self._price_lock = threading.Lock()

        # 엔진 및 클라이언트 (lazy init)
        self._upbit_client = None
        self._relay = None  # OpenClawRelay (파일 기반 IPC)
        self.engine = None

        # 상태
        self._running = False
        self._emergency_stop = False
        self._paused = False  # /pause 명령으로 엔진 사이클 일시정지
        self._last_cycle_run: float = 0.0  # 엔진 실행 간격 추적

        # DB 초기화 확인
        if not Path(db_path).exists():
            logger.warning("DB 없음: %s — 데이터 먼저 수집 필요 (python data_crawler_v2.py)", db_path)

    # ------------------------------------------------------------------
    # WebSocket 가격 수신
    # ------------------------------------------------------------------
    def _on_ticker(self, data: dict) -> None:
        """WebSocket ticker 메시지 핸들러."""
        try:
            ticker = data.get("code", "")
            price = float(data.get("trade_price", 0))
            ts = data.get("trade_timestamp", 0) / 1000.0
            with self._price_lock:
                self._price_cache[ticker] = {"price": price, "timestamp": ts}
        except (ValueError, TypeError) as e:
            logger.debug("ticker 파싱 오류: %s", e)

    def _ws_connect(self) -> None:
        """업비트 WebSocket 연결 루프 (재접속 포함)."""
        import websocket  # 지연 임포트

        codes = self.TICKERS
        ws_url = "wss://api.upbit.com/websocket/v1"

        while self._running and not self._emergency_stop:
            try:
                ws = websocket.WebSocket()
                ws.connect(ws_url, timeout=10)
                # 구독 메시지
                subscribe = json.dumps([
                    {"ticket": "baekdoocoin"},
                    {"type": "ticker", "codes": codes, "is_only_realtime": True},
                ])
                ws.send(subscribe)
                logger.info("WebSocket 연결됨 — %d종 감시 중", len(codes))

                while self._running and not self._emergency_stop:
                    try:
                        ws.settimeout(5)
                        raw = ws.recv()
                        data = json.loads(raw)
                        self._on_ticker(data)

                        # 주기적 엔진 실행
                        self._maybe_run_cycle()

                    except websocket.WebSocketTimeoutException:
                        self._maybe_run_cycle()
                        continue
                    except json.JSONDecodeError:
                        continue

                ws.close()
            except Exception as e:
                if self._running and not self._emergency_stop:
                    logger.error("WebSocket 오류 — 5초 후 재접속: %s", e)
                    time.sleep(5)

    # ------------------------------------------------------------------
    # 엔진 통합
    # ------------------------------------------------------------------
    def _result_as_dict(self, result) -> dict:
        """Normalize engine result to dict (compat: legacy dict vs MTF dataclass)."""
        if isinstance(result, dict):
            return result
        # Convert dataclass to dict-like access
        return {
            "emergency_stop": getattr(result, "emergency_stop", None),
            "orders": [
                {
                    "ticker": o.ticker,
                    "side": o.side,
                    "amount_krw": o.amount_krw,
                    "price": o.price,
                    "strategy": f"mtf-{o.sleeve}",
                    "reason": o.reason,
                    "client_order_id": o.client_order_id or f"baekdoocoin-{o.ticker}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
                }
                for o in getattr(result, "orders", [])
            ],
            "summary": getattr(result, "summary", ""),
            "regimes": getattr(result, "regimes", {}),
        }

    def _maybe_run_cycle(self) -> None:
        """Run engine cycle and execute orders."""
        if self._paused:
            return
        now = time.monotonic()
        if now - self._last_cycle_run < 60.0:
            return
        self._last_cycle_run = now

        try:
            # Update equity from live balance (총 평가액 = 원화 + 코인)
            if self._upbit_client:
                try:
                    live_equity = self._upbit_client.get_total_equity_krw()
                    if live_equity > 0:
                        self.engine.equity_krw = live_equity
                except Exception:
                    pass  # keep cached equity

            # 실시간가 캐시를 엔진에 전달 (당일 돌파 감지용)
            live_prices = {}
            with self._price_lock:
                for ticker, data in self._price_cache.items():
                    if data.get("price", 0) > 0:
                        live_prices[ticker] = data["price"]

            # Run engine cycle with live prices (legacy engine supports live_prices kwarg)
            try:
                raw_result = self.engine.run_cycle(live_prices=live_prices)
            except TypeError:
                raw_result = self.engine.run_cycle()
            result = self._result_as_dict(raw_result)

            if result.get("emergency_stop"):
                self._emergency_stop = True
                if self._relay:
                    self._relay.send_alert(f"🚨 Emergency Stop: {result['emergency_stop']}")
                return

            # Execute orders
            for order in result.get("orders", []):
                self._execute_cycle_order(order)

            # Send summary if there were orders
            if result.get("orders"):
                summary = result.get("summary", "")
                if summary and self._relay:
                    self._relay.send_alert(summary[:1000])

        except Exception as e:
            logger.error("Cycle error: %s", e)

    def _execute_cycle_order(self, order: dict) -> None:
        """Execute a single order generated by engine.run_cycle()."""
        ticker = order["ticker"]
        side = order["side"]
        amount_krw = order["amount_krw"]
        strategy = order.get("strategy", "?")
        reason = order.get("reason", "")
        client_id = order.get("client_order_id", "")

        if not self._upbit_client:
            return

        try:
            if side == "BUY":
                if self.trading_enabled:
                    result = self._upbit_client.market_buy(ticker, amount_krw, client_order_id=client_id)
                    logger.info("BUY %s KRW %d -> %s", ticker, amount_krw, result.get("uuid", "?"))
                if self._relay:
                    self._relay.send_alert(
                        f"{'✅' if self.trading_enabled else '🧪'} BUY {ticker}\n"
                        f"금액: ₩{amount_krw:,}\n전략: {strategy}\n사유: {reason}"
                    )

            elif side == "SELL":
                if self.trading_enabled:
                    sell_result = None
                    try:
                        sell_result = self._upbit_client.market_sell_amount(ticker, amount_krw, client_order_id=client_id)
                    except Exception as sell_err:
                        err_str = str(sell_err).lower()
                        if "under_min_total" in err_str or ("5000" in err_str and "최소" in err_str):
                            logger.warning(
                                "SELL %s amount_krw=%d below min — buy 5k first then sell all",
                                ticker, amount_krw)
                            try:
                                self._upbit_client.market_buy(ticker, 5000)
                                time.sleep(0.5)
                            except Exception as buy_err:
                                logger.warning("Workaround buy failed: %s", buy_err)
                                raise sell_err
                            coin_bal, _ = self._upbit_client.get_coin_balance(ticker)
                            if coin_bal > 0:
                                sell_result = self._upbit_client.market_sell(
                                    ticker, coin_bal, client_order_id=client_id)
                            else:
                                raise sell_err
                        else:
                            raise sell_err
                    if sell_result:
                        logger.info("SELL %s KRW %d -> %s", ticker, amount_krw, sell_result.get("uuid", "?"))
                if self._relay:
                    self._relay.send_alert(
                        f"{'✅' if self.trading_enabled else '🧪'} SELL {ticker}\n"
                        f"금액: ₩{amount_krw:,}\n사유: {reason}"
                    )
        except Exception as e:
            logger.error("Order failed %s %s: %s", side, ticker, e)
            # CTP 부분매도가 실패하면 소비 대기 중인 rung 을 사다리에 되돌린다
            try:
                if side == "SELL" and self.engine is not None:
                    self.engine.risk.restore_pending(ticker)
            except Exception:
                pass
            if self._relay:
                self._relay.send_alert(f"❌ 주문 실패: {ticker} {side} — {e}")

    # ------------------------------------------------------------------
    # 상태 조회 (Telegram /status 용)
    # ------------------------------------------------------------------
    def _get_status(self) -> str:
        """Telegram /status 콜백: 엔진 상태 요약."""
        if self.engine is None:
            return "엔진 미초기화"
        try:
            # Open positions from engine
            open_pos = getattr(self.engine, "_open_positions", {})
            # Latest prices from cache
            with self._price_lock:
                latest_prices = {t: data["price"] for t, data in self._price_cache.items() if data["price"] > 0}
            # Equity
            equity = getattr(self.engine, "equity_krw", 0)
            lines = [
                "📊 백두코인봇 엔진 상태",
                f"자본금: ₩{equity:,.0f}",
                f"열린 포지션: {len(open_pos)}개",
            ]
            for ticker, info in open_pos.items():
                entry_price = info.get("entry_price", 0)
                qty = info.get("qty", 0)
                lines.append(f"  • {ticker}: {qty:.6f} @ ₩{entry_price:,.0f}")
            lines.append("\n💹 최신 가격 (캐시):")
            for ticker in self.TICKERS:
                price = latest_prices.get(ticker, 0.0)
                if price > 0:
                    lines.append(f"  {ticker}: ₩{price:,.0f}")
            return "\n".join(lines)
        except Exception as e:
            return f"상태 조회 오류: {e}"

    # ------------------------------------------------------------------
    # 백그라운드 데이터 업데이트
    # ------------------------------------------------------------------
    def _update_loop(self) -> None:
        """주기적 DB 업데이트 (최신 OHLCV 반영)."""
        while self._running and not self._emergency_stop:
            try:
                import subprocess, sys
                subprocess.run(
                    [sys.executable, "data_updater.py", "--db", self.db_path],
                    cwd=Path(__file__).parent,
                    capture_output=True,
                    timeout=120,
                )
                logger.info("DB 업데이트 완료")
            except Exception as e:
                logger.error("DB 업데이트 오류: %s", e)
            # 다음 업데이트까지 5분 대기
            for _ in range(300):
                if not self._running or self._emergency_stop:
                    break
                time.sleep(1)

    # ------------------------------------------------------------------
    # 잔고 조회 (/balance 명령용)
    # ------------------------------------------------------------------
    def _get_balance(self) -> str:
        """업비트 잔고 요약 문자열 반환."""
        if self._upbit_client is None:
            return "업비트 클라이언트 미초기화"
        try:
            krw = self._upbit_client.get_krw_balance()
            balances = self._upbit_client.get_all_balances()  # {ticker: {qty, avg_price, value_krw}}
            lines = [f"💰 KRW: ₩{krw:,.0f}"]
            total_coin = 0.0
            for ticker, info in sorted(balances.items()):
                qty = info.get("qty", 0)
                value = info.get("value_krw", 0)
                if qty <= 0:
                    continue
                total_coin += value
                lines.append(f"  • {ticker}: {qty:.8f} (≈₩{value:,.0f})")
            lines.append(f"합계 평가액: ₩{krw + total_coin:,.0f}")
            return "\n".join(lines)
        except Exception as e:
            return f"잔고 조회 오류: {e}"

    # ------------------------------------------------------------------
    # 명령 폴링 루프 (OpenClaw 릴레이)
    # ------------------------------------------------------------------
    def _handle_command(self, cmd: dict) -> None:
        """command.json에서 읽은 명령 1건 처리."""
        command = str(cmd.get("command", "")).strip().lstrip("/").lower()
        logger.info("릴레이 명령 처리: %s", command)

        if command == "status":
            self._relay.send_alert(self._get_status())
        elif command == "balance":
            self._relay.send_alert(self._get_balance())
        elif command == "stop":
            self._tg_stop()
            self._relay.send_alert("🚨 비상정지 실행됨 (/stop) — 모든 매매 중단")
        elif command == "resume":
            self._tg_resume()
            self._relay.send_alert("✅ 매매 재개됨 (/resume)")
        elif command == "pause":
            self._paused = True
            self._relay.send_alert("⏸️ 엔진 일시정지 (/pause) — 새 사이클 중단")
        elif command in ("help", "start"):
            self._relay.send_alert(
                "📖 백두코인봇 명령어\n"
                "/status — 엔진 상태\n/balance — 잔고 조회\n"
                "/pause — 일시정지\n/resume — 재개\n/stop — 비상정지"
            )
        else:
            self._relay.send_alert(f"❓ 알 수 없는 명령: /{command}")

    def _command_loop(self) -> None:
        """2초마다 릴레이에서 미처리 명령을 확인해 처리."""
        while self._running:
            try:
                cmd = self._relay.get_pending_command()
                if cmd is not None:
                    self._handle_command(cmd)
            except Exception as e:
                logger.error("명령 루프 오류: %s", e)
            # 2초 대기 (종료 신호 빠르게 감지)
            for _ in range(20):
                if not self._running:
                    break
                time.sleep(0.1)

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------
    def start(self) -> None:
        """라이브 모니터 시작."""
        self._running = True

        # 업비트 클라이언트 초기화
        if self.config.has_upbit():
            from upbit_client import UpbitClient
            self._upbit_client = UpbitClient(
                self.config.upbit_access_key, self.config.upbit_secret_key,
            )
            if self._upbit_client.check_connection():
                krw = self._upbit_client.get_krw_balance()
                logger.info("업비트 연결 성공 — 원화 잔고: ₩%.0f", krw)

        # OpenClaw 릴레이 초기화 (파일 기반 — Telegram 직접 호출 없음)
        from openclaw_relay import OpenClawRelay
        self._relay = OpenClawRelay()
        self._relay.start()
        self._relay.send_alert("🟢 백두코인봇 라이브 모니터 시작됨 (OpenClaw 릴레이)\n감시: 10종, 거래: %s" %
                              ("활성" if self.trading_enabled else "비활성"))

        # 엔진 초기화 — MTF (1D+5M Dual Sleeve) first, fallback to legacy
        # 초기 equity는 원화 현금 + 보유 코인 평가액 합산 (총 평가액 기준)
        init_equity = 1_000_000
        if self._upbit_client:
            try:
                total_equity = self._upbit_client.get_total_equity_krw()
                if total_equity > 0:
                    init_equity = total_equity
            except Exception:
                pass

        # Use MTF engine (5M data capped at 500 bars, ~1s per cycle)
        engine_type = "legacy"
        try:
            from engine_mtf import MTFEngine
            self.engine = MTFEngine(
                db_path=self.db_path,
                tickers=self.TICKERS,
                equity_krw=init_equity,
            )
            engine_type = "MTF (1D+5M)"
            # Sync actual exchange positions into engine
            if self._upbit_client:
                n = self.engine.sync_from_exchange(self._upbit_client)
                if n > 0:
                    logger.info("Exchange sync: %d positions loaded", n)
        except ImportError as e:
            logger.warning("MTF not available (%s), falling back to legacy", e)
            from engine import 백두코인봇Engine
            self.engine = 백두코인봇Engine(
                db_path=self.db_path,
                tickers=self.TICKERS,
                equity_krw=init_equity,
            )
        logger.info("Engine initialized: %s (equity=₩%s)", engine_type, f"{init_equity:,}")

        # WebSocket 스레드
        ws_thread = threading.Thread(target=self._ws_connect, daemon=True, name="baekdoocoin-ws")
        ws_thread.start()

        # DB 업데이트 스레드
        update_thread = threading.Thread(target=self._update_loop, daemon=True, name="baekdoocoin-update")
        update_thread.start()

        # 명령 폴링 스레드 (OpenClaw 릴레이)
        cmd_thread = threading.Thread(target=self._command_loop, daemon=True, name="baekdoocoin-cmd")
        cmd_thread.start()

        logger.info("백두코인봇 라이브 모니터 시작 — 10종 실시간 감시, 거래: %s",
                    "활성" if self.trading_enabled else "비활성")

    def stop(self) -> None:
        """정상 종료."""
        self._running = False
        if self._relay:
            self._relay.send_alert("🔴 백두코인봇 모니터 종료됨")
            self._relay.stop()
        logger.info("백두코인봇 모니터 종료")

    # 텔레그램 콜백
    def _tg_stop(self) -> None:
        self._emergency_stop = True
        Path("/tmp/baekdoocoin_emergency_stop").touch()
        logger.warning("🚨 텔레그램 비상정지 명령 수신")

    def _tg_resume(self) -> None:
        self._emergency_stop = False
        p = Path("/tmp/baekdoocoin_emergency_stop")
        if p.exists():
            p.unlink()
        logger.info("✅ 텔레그램 재개 명령 수신")


if __name__ == "__main__":
    import argparse
    import sys
    import os

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config

    parser = argparse.ArgumentParser(description="백두코인봇 라이브 모니터")
    parser.add_argument("--trade", action="store_true", help="자동매매 활성화")
    parser.add_argument("--db", default="data/upbit_v2.db", help="DB 경로")
    args = parser.parse_args()

    # Write PID file for watchdog
    pidfile = "/tmp/baekdoocoin_live_monitor.pid"
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    cfg = Config.load()
    monitor = LiveMonitor(cfg, db_path=args.db, trading_enabled=args.trade)
    monitor.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
    finally:
        # Clean up PID file on exit
        try:
            os.remove(pidfile)
        except OSError:
            pass