"""
백두코인봇 메인 트레이딩 엔진
=========================

시장 레짐 감지 → 전략 선택 → 신호 확인 → 주문 생성
파이프라인을 관리하는 메인 엔진입니다.

사용법:
    engine = 백두코인봇Engine(db_path="data/upbit.db", tickers=["KRW-BTC", "KRW-ETH"], equity_krw=10_000_000)
    orders = engine.run_cycle()

참조:
    - strategy_research.md — 백두코인봇 추천 하이브리드 아키텍처
"""

import logging
import os
import signal
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

try:
    from .regime_detector import RegimeDetector, MarketRegime
    from .turtle import TurtleStrategy
    from .squeeze_momentum import SqueezeMomentumStrategy
    from .bb_rsi_reversal import BBRSIReversalStrategy
    from .risk import RiskManager, make_position, RiskAction
except ImportError:
    from strategies.regime_detector import RegimeDetector, MarketRegime
    from strategies.turtle import TurtleStrategy
    from strategies.squeeze_momentum import SqueezeMomentumStrategy
    from strategies.bb_rsi_reversal import BBRSIReversalStrategy
    from strategies.risk import RiskManager, make_position, RiskAction

logger = logging.getLogger(__name__)


class 백두코인봇Engine:
    """백두코인봇 메인 트레이딩 엔진.

    시장 레짐에 따라 최적의 전략을 선택하고,
    신호를 확인하여 주문 리스트를 생성합니다.

    하이브리드 아키텍처:
        TRENDING  → Turtle (1D, 55일 Donchian)
        SQUEEZING → Squeeze Momentum (1H, BB/KC)
        REVERSING → BB+RSI Reversal (1H, 극단값)
        NEUTRAL   → 관망 (No orders)
    """

    # Kill-switch file path — creating this file stops the engine on next cycle
    EMERGENCY_STOP_FILE = "/tmp/baekdoocoin_emergency_stop"
    # Max orders generated per single cycle (safety cap)
    DEFAULT_MAX_ORDERS_PER_CYCLE = 10

    def __init__(
        self,
        db_path: str,
        tickers: list[str],
        equity_krw: float,
        regime_detector: Optional[RegimeDetector] = None,
        max_orders_per_cycle: int = DEFAULT_MAX_ORDERS_PER_CYCLE,
        max_daily_trades: int = 20,
        max_daily_loss_pct: float = 0.05,
    ):
        """
        Parameters
        ----------
        db_path : str
            SQLite 데이터베이스 파일 경로.
        tickers : list[str]
            모니터링할 업비트 티커 목록 (예: ["KRW-BTC", "KRW-ETH"]).
        equity_krw : float
            계좌 잔고 (KRW), must be positive.
        regime_detector : RegimeDetector, optional
            커스텀 레짐 감지기. None이면 기본값 사용.
        max_orders_per_cycle : int
            사이클당 최대 주문 수 (기본값: 10). 초과 시 EmergencyStop.
        max_daily_trades : int
            하루 최대 거래 횟수 (기본값: 20). 초과 시 신호 무시.
        max_daily_loss_pct : float
            하루 최대 손실률 (기본값: 0.05 = 5%). 초과 시 EmergencyStop.
        """
        # --- Startup validation ---
        if not tickers:
            raise ValueError("tickers list cannot be empty")
        if equity_krw <= 0:
            raise ValueError(f"equity_krw must be positive, got {equity_krw}")
        if max_orders_per_cycle < 1:
            raise ValueError(f"max_orders_per_cycle must be >= 1, got {max_orders_per_cycle}")

        self.db_path = db_path
        self.tickers = tickers
        self.equity_krw = equity_krw
        self.regime_detector = regime_detector or RegimeDetector()
        self.max_orders_per_cycle = max_orders_per_cycle
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_pct = max_daily_loss_pct

        # 전략 인스턴스 (티커별로 필요할 때 생성)
        self._strategies: dict[str, dict] = {}

        # 리스크 매니저 — 물타기/불타기/뼈바르기 통합
        self.risk = RiskManager()

        # BUG-19 FIX: 엔진이 자체적으로 열린 포지션을 티커별로 추적해야 한다.
        # 이전에는 run_cycle이 check_entry만 호출하고 check_exit는 전혀
        # 호출하지 않아서, 매 사이클마다 이미 보유 중인 티커에도 계속 BUY
        # 주문이 생성되고 포지션을 청산하는 주문은 절대 생성되지 않았다.
        # 구조: { ticker: make_position() dict }
        self._open_positions: dict[str, dict] = {}

        # Daily trade tracking (reset at midnight)
        self._daily_trades_count: int = 0
        self._daily_last_date: str = ""
        self._daily_starting_equity: float = equity_krw
        self._pos_lock = threading.Lock()  # thread safety for _open_positions

        # --- Emergency stop state ---
        self._emergency_stop_active = False

        # Register signal handlers for graceful shutdown
        self._setup_signal_handlers()

        logger.info(
            "BaekdooCoin engine init: tickers=%d, equity=%d, "
            "max_orders/cycle=%d, max_daily_trades=%d, max_daily_loss=%.0f%%",
            len(tickers), int(equity_krw),
            max_orders_per_cycle, max_daily_trades, max_daily_loss_pct * 100,
        )

    def _setup_signal_handlers(self):
        """Register SIGINT/SIGTERM handlers for graceful shutdown."""
        def _handle_shutdown(signum, frame):
            logger.warning(
                "신호 %s 수신 — 비상 정지 활성화",
                signal.Signals(signum).name,
            )
            self._emergency_stop_active = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle_shutdown)
            except ValueError:
                pass  # not in main thread — acceptable

    # ------------------------------------------------------------------
    # 데이터 로딩
    # ------------------------------------------------------------------
    def _load_data(self, ticker: str, timeframe: str) -> pd.DataFrame:
        """지정 티커/타임프레임의 OHLCV 데이터를 로드.

        Parameters
        ----------
        ticker : str
            업비트 티커.
        timeframe : str
            "1h" 또는 "1d".

        Returns
        -------
        pd.DataFrame
            OHLCV DataFrame.
        """
        table_map = {"1h": "ohlcv_1h", "1d": "ohlcv_1d"}
        table = table_map.get(timeframe, "ohlcv_1h")

        conn = sqlite3.connect(self.db_path)
        query = f"""
            SELECT timestamp, open, high, low, close, volume, volume_krw
            FROM {table}
            WHERE ticker = ?
            ORDER BY timestamp ASC
        """
        df = pd.read_sql_query(query, conn, params=(ticker,))
        conn.close()

        if df.empty:
            raise ValueError(f"데이터 없음: ticker={ticker}, timeframe={timeframe}")

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    def _get_or_create_strategy(self, ticker: str, strategy_name: str):
        """전략 인스턴스를 가져오거나 생성 (캐싱).

        Parameters
        ----------
        ticker : str
            업비트 티커.
        strategy_name : str
            "turtle", "squeeze_momentum", "bb_rsi_reversal" 중 하나.

        Returns
        -------
        전략 인스턴스.
        """
        if ticker not in self._strategies:
            self._strategies[ticker] = {}

        if strategy_name not in self._strategies[ticker]:
            if strategy_name == "turtle":
                self._strategies[ticker][strategy_name] = TurtleStrategy(
                    self.db_path, ticker
                )
            elif strategy_name == "squeeze_momentum":
                self._strategies[ticker][strategy_name] = SqueezeMomentumStrategy(
                    self.db_path, ticker
                )
            elif strategy_name == "bb_rsi_reversal":
                self._strategies[ticker][strategy_name] = BBRSIReversalStrategy(
                    self.db_path, ticker
                )
            else:
                raise ValueError(f"알 수 없는 전략: {strategy_name}")

        return self._strategies[ticker][strategy_name]

    # ------------------------------------------------------------------
    # 메인 사이클
    # ------------------------------------------------------------------
    def _check_emergency_stop(self) -> Optional[str]:
        """Check all emergency-stop conditions. Returns reason if stop is triggered."""
        # 1. File-based kill switch
        if os.path.exists(self.EMERGENCY_STOP_FILE):
            return f"Kill-switch file detected: {self.EMERGENCY_STOP_FILE}"
        # 2. Internal emergency flag (set by signal handler)
        if self._emergency_stop_active:
            return "Emergency stop activated by signal handler"
        # 3. Daily loss limit
        if self.equity_krw < self._daily_starting_equity * (1 - self.max_daily_loss_pct):
            loss_pct = (1 - self.equity_krw / self._daily_starting_equity) * 100
            return (
                f"Daily loss limit exceeded: -{loss_pct:.1f}% "
                f"(limit: {self.max_daily_loss_pct*100:.0f}%)"
            )
        # 4. Daily trade count limit
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_last_date:
            self._daily_trades_count = 0
            self._daily_last_date = today
            self._daily_starting_equity = self.equity_krw
        if self._daily_trades_count >= self.max_daily_trades:
            return f"Daily trade limit reached ({self.max_daily_trades})"
        return None  # All clear

    def _persist_positions(self):
        """Persist open positions state to a JSON file for crash recovery.

        On next startup, load this file to restore position tracking.
        """
        import json
        pos_file = os.path.join(os.path.dirname(self.db_path), "open_positions.json")
        try:
            with open(pos_file, "w") as f:
                json.dump(self._open_positions, f, indent=2, default=str)
        except OSError as e:
            logger.warning("Failed to persist positions: %s", e)

    def _load_persisted_positions(self):
        """Restore open positions from persisted JSON file (crash recovery)."""
        import json
        pos_file = os.path.join(os.path.dirname(self.db_path), "open_positions.json")
        if not os.path.exists(pos_file):
            return
        try:
            with open(pos_file) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                with self._pos_lock:
                    self._open_positions.update(saved)
                logger.info(
                    "복구: %d개 포지션 상태 복원됨", len(saved),
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load persisted positions: %s", e)

    def register_manual_position(self, ticker: str, entry_price: float,
                                  amount_krw: int, strategy: str = "manual",
                                  atr: Optional[float] = None):
        """수동 매수한 포지션을 엔진에 등록 — 물타기/불타기/뼈바르기 대상 포함.

        업비트에서 직접 매수한 코인을 엔진이 인식하게 합니다.
        이미 등록된 티커는 무시.

        Parameters
        ----------
        ticker : str
            업비트 티커 (예: "KRW-XRP").
        entry_price : float
            매수 평균단가.
        amount_krw : int
            매수 금액 (KRW).
        strategy : str
            전략명 (기본값: "manual").
        atr : float, optional
            ATR 값. None이면 가격의 2%로 추정.
        """
        with self._pos_lock:
            if ticker in self._open_positions:
                logger.info("register_manual_position: %s already tracked, skipping", ticker)
                return
            atr_val = atr if atr and atr > 0 else entry_price * 0.02
            self._open_positions[ticker] = make_position(
                entry_price=entry_price,
                atr=atr_val,
                amount_krw=amount_krw,
                strategy=strategy,
                current_price=entry_price,
            )
        self._persist_positions()
        # logger.info("📝 수동 포지션 등록: %s ₩%d @ ₩%,.0f (전략: %s)",
                    # ticker, amount_krw, entry_price, strategy)
        logger.info("📝 수동 포지션 등록: %s ₩%d @ ₩%d (전략: %s)",
                    ticker, amount_krw, int(entry_price), strategy)

    def run_cycle(self, live_prices: Optional[dict[str, float]] = None) -> dict:
        """한 번의 트레이딩 사이클을 실행합니다.

        각 티커별로:
            0. 비상 정지 조건 확인
            1. 시장 레짐 감지
            2. 레짐에 맞는 전략 선택
            3. 진입/청산 신호 확인
            4. 주문 생성

        Parameters
        ----------
        live_prices : dict[str, float], optional
            실시간 가격 캐시 {ticker: price}. 1일봉에 당일 봉이 없을 때
            실시간 가격으로 임시 봉을 추가해 돌파 감지 정확도를 높임.

        Returns
        -------
        dict
            {
                "timestamp": str (현재 시간),
                "equity_krw": float,
                "regimes": dict (ticker → regime),
                "orders": list[dict],
                "summary": str,
                "emergency_stop": str | None
            }
        """
        live_prices = live_prices or {}
        # --- Emergency stop check ---
        stop_reason = self._check_emergency_stop()
        if stop_reason is not None:
            logger.critical("🚨 비상 정지: %s", stop_reason)
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "equity_krw": self.equity_krw,
                "regimes": {},
                "orders": [],
                "summary": f"🚨 EMERGENCY STOP: {stop_reason}",
                "emergency_stop": stop_reason,
            }

        timestamp = datetime.now(timezone.utc).isoformat()
        orders = []
        regimes = {}
        summary_lines = []

        for ticker in self.tickers:
            try:
                # 1. 레짐 감지 (1시간봉 기준)
                df_1h = self._load_data(ticker, "1h")
                regime, indicators = self.regime_detector.detect_regime(df_1h)
                regimes[ticker] = {
                    "regime": regime.value,
                    "indicators": indicators,
                }

                logger.info(
                    "%s → %s (ADX=%.1f, RSI=%.1f, BB_width=%.4f)",
                    ticker, regime.value,
                    indicators["adx"], indicators["rsi"], indicators["bb_width"],
                )

                # 🦴 RiskManager 기반 포지션 관리 — 물타기/불타기/뼈바르기 통합
                # 실시간가 우선 사용
                live_price = live_prices.get(ticker)
                current_price_for_risk = live_price if live_price and live_price > 0 else float(df_1h["close"].iloc[-1])

                with self._pos_lock:
                    open_pos = self._open_positions.get(ticker)
                if open_pos is not None:
                    # 현재가 가져오기 (실시간가 우선)
                    current_price = current_price_for_risk
                    # 1유닛 = 계좌의 1% (기본)
                    unit_size_krw = max(int(self.equity_krw * 0.01), 5_000)

                    # RiskManager 평가
                    decision = self.risk.evaluate(open_pos, current_price, unit_size_krw)

                    if decision.action == RiskAction.SELL_ALL:
                        orders.append({
                            "ticker": ticker,
                            "side": "SELL",
                            "strategy": open_pos.get("strategy", "?"),
                            "client_order_id": f"baekdoocoin-{ticker}-{timestamp[:10]}-{len(orders):04d}",
                            "amount_krw": int(open_pos.get("amount_krw", 0)),
                            "price": current_price,
                            "reason": decision.reason,
                        })
                        summary_lines.append(f"📭 {ticker} SELL_ALL: {decision.reason}")
                        with self._pos_lock:
                            del self._open_positions[ticker]
                        self._persist_positions()
                        self._daily_trades_count += 1

                    elif decision.action == RiskAction.SELL_PARTIAL:
                        sell_krw = decision.amount_krw
                        orders.append({
                            "ticker": ticker,
                            "side": "SELL",
                            "strategy": open_pos.get("strategy", "?"),
                            "client_order_id": f"baekdoocoin-{ticker}-{timestamp[:10]}-{len(orders):04d}",
                            "amount_krw": sell_krw,
                            "price": current_price,
                            "reason": decision.reason,
                        })
                        summary_lines.append(f"🦴 {ticker} SELL_PARTIAL: {decision.reason}")
                        # 포지션 업데이트
                        with self._pos_lock:
                            open_pos["amount_krw"] = max(0, open_pos["amount_krw"] - sell_krw)
                            open_pos["exit_stage"] = open_pos.get("exit_stage", 0) + 1
                            if "Bone Carve" in decision.reason:
                                open_pos["bone_carve_count"] = open_pos.get("bone_carve_count", 0) + 1
                                open_pos["last_carve_price"] = current_price
                        self._persist_positions()
                        self._daily_trades_count += 1

                    elif decision.action == RiskAction.BUY_DOWN:
                        add_krw = decision.amount_krw
                        orders.append({
                            "ticker": ticker,
                            "side": "BUY",
                            "strategy": open_pos.get("strategy", "?"),
                            "client_order_id": f"baekdoocoin-{ticker}-{timestamp[:10]}-{len(orders):04d}",
                            "amount_krw": add_krw,
                            "price": current_price,
                            "reason": decision.reason,
                        })
                        summary_lines.append(f"💧 {ticker} 물타기: {decision.reason}")
                        with self._pos_lock:
                            open_pos["amount_krw"] = open_pos.get("amount_krw", 0) + add_krw
                            open_pos["last_buy_price"] = current_price
                            open_pos["stage"] = open_pos.get("stage", 1) + 1
                        self._persist_positions()
                        self._daily_trades_count += 1

                    elif decision.action == RiskAction.BUY_UP:
                        orders.append({
                            "ticker": ticker,
                            "side": "BUY",
                            "strategy": open_pos.get("strategy", "?"),
                            "client_order_id": f"baekdoocoin-{ticker}-{timestamp[:10]}-{len(orders):04d}",
                            "amount_krw": decision.amount_krw,
                            "price": current_price,
                            "reason": decision.reason,
                        })
                        summary_lines.append(f"🔥 {ticker} 불타기: {decision.reason}")
                        with self._pos_lock:
                            open_pos["amount_krw"] = open_pos.get("amount_krw", 0) + decision.amount_krw
                            open_pos["last_buy_price"] = current_price
                            open_pos["fire_count"] = open_pos.get("fire_count", 0) + 1
                        self._persist_positions()
                        self._daily_trades_count += 1

                    else:
                        summary_lines.append(
                            f"💼 {ticker}: 보유 중 ({open_pos.get('strategy', '?')}), "
                            f"{decision.reason}"
                        )
                    continue  # 포지션 관리 완료, 진입 로직 스킵

                # 2. 레짐에 따른 전략 선택 및 신호 확인 (포지션이 없을 때만)
                if regime == MarketRegime.TRENDING:
                    # Turtle — 1일봉 기준, 실시간가 반영하여 당일 돌파 즉시 감지
                    strategy = self._get_or_create_strategy(ticker, "turtle")
                    df_1d = self._load_data(ticker, "1d")
                    # 실시간가로 당일 미완성 봉을 덮어쓰기 (당일 돌파 감지)
                    live_price = live_prices.get(ticker)
                    if live_price and live_price > 0:
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        last_idx = df_1d.index[-1]
                        if str(last_idx.date()) != today_str:
                            fake_row = pd.DataFrame([{
                                "open": live_price, "high": live_price,
                                "low": live_price, "close": live_price,
                                "volume": 0, "volume_krw": 0,
                            }], index=[pd.Timestamp(today_str)])
                            df_1d = pd.concat([df_1d, fake_row])
                    entry = strategy.check_entry(df_1d)

                    if entry["signal"] == "LONG":
                        pos_size = strategy.calculate_position_size(
                            self.equity_krw, entry["atr"], entry["price"]
                        )
                        # Cap position size at remaining equity
                        pos_size = min(pos_size, int(self.equity_krw))
                        orders.append({
                            "ticker": ticker,
                            "side": "BUY",
                            "strategy": "turtle",
                            # Generate unique client_order_id for idempotency
                            "client_order_id": (
                                f"baekdoocoin-{ticker}-{timestamp[:10]}-"
                                f"{len(orders):04d}"
                            ),
                            "amount_krw": pos_size,
                            "price": entry["price"],
                            "reason": entry["reason"],
                        })
                        summary_lines.append(
                            f"🐢 {ticker} LONG: {entry['reason']} "
                            f"(사이즈: ₩{pos_size:,})"
                        )
                        # 🦴 RiskManager 기반 포지션 초기화 (물타기/불타기/뼈바르기 지원)
                        with self._pos_lock:
                            self._open_positions[ticker] = make_position(
                                entry_price=entry["price"],
                                atr=entry.get("atr", entry["price"] * 0.02),
                                amount_krw=pos_size,
                                strategy="turtle",
                                current_price=entry["price"],
                            )
                        self._persist_positions()
                        self._daily_trades_count += 1

                elif regime == MarketRegime.SQUEEZING:
                    # Squeeze Momentum — 1시간봉 기준
                    strategy = self._get_or_create_strategy(
                        ticker, "squeeze_momentum"
                    )
                    entry = strategy.check_entry(df_1h)

                    if entry["signal"] == "LONG":
                        # Squeeze는 포지션 사이징을 등분산 방식으로
                        # 계좌의 1% 리스크
                        pos_size = int(self.equity_krw * 0.01)
                        pos_size = min(max(pos_size, 5_000), int(self.equity_krw))
                        orders.append({
                            "ticker": ticker,
                            "side": "BUY",
                            "strategy": "squeeze_momentum",
                            "client_order_id": (
                                f"baekdoocoin-{ticker}-{timestamp[:10]}-"
                                f"{len(orders):04d}"
                            ),
                            "amount_krw": pos_size,
                            "price": entry["price"],
                            "reason": entry["reason"],
                        })
                        summary_lines.append(
                            f"📦 {ticker} LONG: {entry['reason']}"
                        )
                        with self._pos_lock:
                            self._open_positions[ticker] = make_position(
                                entry_price=entry["price"],
                                atr=entry.get("momentum", entry["price"] * 0.02),
                                amount_krw=pos_size,
                                strategy="squeeze_momentum",
                                current_price=entry["price"],
                            )
                        self._persist_positions()
                        self._daily_trades_count += 1

                elif regime == MarketRegime.REVERSING:
                    # BB+RSI Reversal — 1시간봉 기준
                    strategy = self._get_or_create_strategy(
                        ticker, "bb_rsi_reversal"
                    )
                    entry = strategy.check_entry(df_1h)

                    if entry["signal"] == "LONG":
                        pos_size = int(self.equity_krw * 0.01)
                        pos_size = min(max(pos_size, 5_000), int(self.equity_krw))
                        orders.append({
                            "ticker": ticker,
                            "side": "BUY",
                            "strategy": "bb_rsi_reversal",
                            "client_order_id": (
                                f"baekdoocoin-{ticker}-{timestamp[:10]}-"
                                f"{len(orders):04d}"
                            ),
                            "amount_krw": pos_size,
                            "price": entry["price"],
                            "reason": entry["reason"],
                        })
                        summary_lines.append(
                            f"🔄 {ticker} LONG: {entry['reason']}"
                        )
                        with self._pos_lock:
                            self._open_positions[ticker] = make_position(
                                entry_price=entry["price"],
                                atr=entry["price"] * 0.02,
                                amount_krw=pos_size,
                                strategy="bb_rsi_reversal",
                                current_price=entry["price"],
                            )
                        self._persist_positions()
                        self._daily_trades_count += 1

                else:
                    # NEUTRAL — 1D DC55 돌파 체크 (레짐 무시하고 터틀 진입)
                    # 레짐 디텍터가 NEUTRAL로 판정해도 1일봉 55일 최고가 돌파면 진입
                    try:
                        strategy = self._get_or_create_strategy(ticker, "turtle")
                        df_1d = self._load_data(ticker, "1d")
                        live_price = live_prices.get(ticker)
                        if live_price and live_price > 0:
                            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                            last_idx = df_1d.index[-1]
                            if str(last_idx.date()) != today_str:
                                fake_row = pd.DataFrame([{
                                    "open": live_price, "high": live_price,
                                    "low": live_price, "close": live_price,
                                    "volume": 0, "volume_krw": 0,
                                }], index=[pd.Timestamp(today_str)])
                                df_1d = pd.concat([df_1d, fake_row])
                        entry = strategy.check_entry(df_1d)
                        if entry["signal"] == "LONG":
                            pos_size = int(self.equity_krw * 0.01)
                            pos_size = min(max(pos_size, 5_000), int(self.equity_krw))
                            orders.append({
                                "ticker": ticker,
                                "side": "BUY",
                                "strategy": "turtle",
                                "client_order_id": f"baekdoocoin-{ticker}-{timestamp[:10]}-{len(orders):04d}",
                                "amount_krw": pos_size,
                                "price": entry["price"],
                                "reason": entry["reason"],
                            })
                            summary_lines.append(
                                f"🐢 {ticker} LONG (DC Override): {entry['reason']} "
                                f"(사이즈: ₩{pos_size:,})"
                            )
                            with self._pos_lock:
                                self._open_positions[ticker] = make_position(
                                    entry_price=entry["price"],
                                    atr=entry.get("atr", entry["price"] * 0.02),
                                    amount_krw=pos_size,
                                    strategy="turtle",
                                    current_price=entry["price"],
                                )
                            self._persist_positions()
                            self._daily_trades_count += 1
                        else:
                            summary_lines.append(f"⏸️ {ticker}: NEUTRAL (관망)")
                    except Exception:
                        summary_lines.append(f"⏸️ {ticker}: NEUTRAL (관망)")

            except Exception as e:
                logger.error("%s 처리 중 오류: %s", ticker, e)
                regimes[ticker] = {"regime": "ERROR", "indicators": {"error": str(e)}}
                summary_lines.append(f"❌ {ticker}: 오류 - {e}")

        # --- Per-cycle order limit ---
        if len(orders) > self.max_orders_per_cycle:
            logger.critical(
                "🚨 사이클당 주문 한도 초과: %d > %d — 비상 정지",
                len(orders), self.max_orders_per_cycle,
            )
            self._emergency_stop_active = True
            return {
                "timestamp": timestamp,
                "equity_krw": self.equity_krw,
                "regimes": regimes,
                "orders": [],
                "summary": (
                    f"🚨 EMERGENCY STOP: orders/cycle limit exceeded "
                    f"({len(orders)} > {self.max_orders_per_cycle})"
                ),
                "emergency_stop": "orders_per_cycle_limit",
            }

        summary = "\n".join(summary_lines) if summary_lines else "신호 없음"

        result = {
            "timestamp": timestamp,
            "equity_krw": self.equity_krw,
            "regimes": regimes,
            "orders": orders,
            "summary": summary,
            "emergency_stop": None,
        }

        logger.info("Cycle 완료: %d 주문 생성", len(orders))
        return result

    def generate_orders(self) -> list[dict]:
        """주문만 생성하는 편의 메서드.

        Returns
        -------
        list[dict]
            주문 리스트.
        """
        cycle = self.run_cycle()
        return cycle["orders"]


# ---------------------------------------------------------------------------
# 데모 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys

    _ROOT = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _ROOT)

    DB_PATH = os.path.join(_ROOT, "data", "upbit.db")

    print("=" * 60)
    print("  ⚙️  백두코인봇 Trading Engine — 데모")
    print("=" * 60)

    # 주요 티커 5개로 데모
    tickers = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-DOGE", "KRW-XRP"]
    equity = 10_000_000  # 1,000만원

    engine = 백두코인봇Engine(
        db_path=DB_PATH,
        tickers=tickers,
        equity_krw=equity,
    )

    print(f"\n📊 모니터링 티커: {len(tickers)}개")
    print(f"💰 계좌 잔고: ₩{equity:,.0f}")
    print(f"\n🔄 트레이딩 사이클 실행 중...\n")

    result = engine.run_cycle()

    print("=" * 60)
    print("  📋 레짐 감지 결과")
    print("=" * 60)
    for ticker, reg in result["regimes"].items():
        if reg["regime"] == "ERROR":
            print(f"  ❌ {ticker}: {reg['indicators'].get('error', '')}")
            continue
        ind = reg["indicators"]
        emoji = {
            "TRENDING": "📈", "SQUEEZING": "📦",
            "REVERSING": "🔄", "NEUTRAL": "⏸️",
        }
        print(f"  {emoji.get(reg['regime'], '❓')} {ticker}: {reg['regime']}")
        print(f"     ADX={ind.get('adx','-')}, RSI={ind.get('rsi','-')}, "
              f"BB_Width={ind.get('bb_width','-')} "
              f"(pct={ind.get('bb_width_pct','-')})")

    print(f"\n{'='*60}")
    print(f"  📝 주문 요약")
    print(f"{'='*60}")
    print(result["summary"])

    if result["orders"]:
        print(f"\n{'='*60}")
        print(f"  🛒 생성된 주문 ({len(result['orders'])}건)")
        print(f"{'='*60}")
        for order in result["orders"]:
            print(f"  [{order['strategy']}] {order['ticker']} {order['side']} "
                  f"₩{order['amount_krw']:,} @ {order['price']:,}")
            print(f"  → {order['reason']}")
    else:
        print("\n  주문 없음 (관망 또는 신호 미발생)")

    print(f"\n✅ 백두코인봇 Engine 데모 완료")