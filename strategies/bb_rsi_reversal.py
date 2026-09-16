"""
볼린저 밴드 + RSI 극단값 평균회귀 전략
=====================================

가격이 볼린저 밴드를 극단적으로 벗어나고 RSI가 과매수/과매도일 때,
반대 방향으로 평균회귀를 노리는 전략.

핵심 아이디어:
    - Long: 가격 < Lower BB + RSI < 25 + Bullish Divergence
    - Exit: 가격이 Middle BB 도달 또는 RSI > 50
    - BB Width가 좁으면 Squeeze 상태 → 진입 금지 (변동성 수축)

참조:
    - strategy_research.md — BB+RSI Extreme 상세 스펙
    - BullCryptoSignals, TradingView Community
"""

import logging
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class BBRSIReversalStrategy:
    """볼린저 밴드 + RSI 극단값 평균회귀 전략.

    가격이 극단적으로 벗어났을 때 반대 방향으로 진입하는
    평균회귀 (Mean Reversion) 전략입니다.
    """

    def __init__(
        self,
        db_path: str,
        ticker: str,
        bb_length: int = 30,
        bb_mult: float = 2.0,
        rsi_length: int = 13,
        rsi_oversold: int = 25,
        rsi_overbought: int = 75,
    ):
        """
        Parameters
        ----------
        db_path : str
            SQLite 데이터베이스 파일 경로.
        ticker : str
            업비트 티커 (예: "KRW-BTC").
        bb_length : int
            볼린저 밴드 기간 (기본값: 30).
        bb_mult : float
            볼린저 밴드 표준편차 승수 (기본값: 2.0).
        rsi_length : int
            RSI 계산 기간 (기본값: 13).
        rsi_oversold : int
            과매도 기준 (기본값: 25).
        rsi_overbought : int
            과매수 기준 (기본값: 75).
        """
        self.db_path = db_path
        self.ticker = ticker
        self.bb_length = bb_length
        self.bb_mult = bb_mult
        self.rsi_length = rsi_length
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        logger.info(
            "BB+RSI 초기화: ticker=%s, BB(%d,%.1f), RSI(%d,<%d/>%d)",
            ticker, bb_length, bb_mult, rsi_length, rsi_oversold, rsi_overbought,
        )

    # ------------------------------------------------------------------
    # 데이터 로딩
    # ------------------------------------------------------------------
    def load_data(self, timeframe: str = "1h") -> pd.DataFrame:
        """지정 타임프레임의 OHLCV 데이터를 DB에서 로드.

        Parameters
        ----------
        timeframe : str
            "1h" 또는 "4h".

        Returns
        -------
        pd.DataFrame
            인덱스는 timestamp (datetime64).
        """
        table_map = {"1h": "ohlcv_1h", "4h": "ohlcv_4h"}
        table = table_map.get(timeframe, "ohlcv_1h")

        conn = sqlite3.connect(self.db_path)
        query = f"""
            SELECT timestamp, open, high, low, close, volume, volume_krw
            FROM {table}
            WHERE ticker = ?
            ORDER BY timestamp ASC
        """
        df = pd.read_sql_query(query, conn, params=(self.ticker,))
        conn.close()

        if df.empty:
            raise ValueError(
                f"{timeframe} 데이터가 없습니다: ticker={self.ticker}"
            )

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    @staticmethod
    def compute_bb(df: pd.DataFrame, length: int = 30, mult: float = 2.0) -> pd.DataFrame:
        """볼린저 밴드 계산.

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        length : int
            이동평균 기간.
        mult : float
            표준편차 승수.

        Returns
        -------
        pd.DataFrame
            'bb_middle', 'bb_upper', 'bb_lower', 'bb_width' 컬럼 추가.
        """
        df = df.copy()
        df["bb_middle"] = df["close"].rolling(window=length).mean()
        std = df["close"].rolling(window=length).std()
        df["bb_upper"] = df["bb_middle"] + mult * std
        df["bb_lower"] = df["bb_middle"] - mult * std
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]
        return df

    @staticmethod
    def compute_rsi(df: pd.DataFrame, period: int = 13) -> pd.Series:
        """RSI (Relative Strength Index) 직접 계산.

        Wilders Smoothing 방식을 사용합니다.

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        period : int
            RSI 계산 기간 (기본값: 13).

        Returns
        -------
        pd.Series
            RSI 값 (0~100).
        """
        df = df.copy()
        delta = df["close"].diff()

        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        # Wilders Smoothing: 첫 번째 값은 단순 평균, 이후는 EMA 형태
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()

        for i in range(period, len(avg_gain)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    # ------------------------------------------------------------------
    # Bullish Divergence 감지
    # ------------------------------------------------------------------
    def _detect_bullish_divergence(self, df: pd.DataFrame) -> bool:
        """Bullish Divergence 감지 (가격 저점 하락, RSI 저점 상승).

        최근 20봉 내에서 확인.

        Parameters
        ----------
        df : pd.DataFrame
            BB와 RSI가 계산된 DataFrame.

        Returns
        -------
        bool
            Bullish Divergence 존재 여부.
        """
        lookback = min(20, len(df) - 1)
        if lookback < 5:
            return False

        rsi = df["rsi"].iloc[-lookback:].values
        price = df["close"].iloc[-lookback:].values

        # 최근 저점 찾기 (단순: 마지막 N개 중 최소값 위치)
        rsi_min_idx = np.argmin(rsi)
        price_min_idx = np.argmin(price)

        # Bullish Divergence: 가격 저점은 더 낮아졌는데 RSI 저점은 더 높아짐
        # → RSI 저점이 가격 저점보다 나중에 발생 + RSI 저점이 이전 저점보다 높음
        if rsi_min_idx > price_min_idx:
            # RSI 저점이 가장 최근이면 Divergence로 간주
            if rsi_min_idx >= len(rsi) - 3:
                # RSI가 과매도 영역 근처인지 확인
                if rsi[rsi_min_idx] < 35:
                    return True

        return False

    # ------------------------------------------------------------------
    # 진입/청산 신호
    # ------------------------------------------------------------------
    def check_entry(self, df: pd.DataFrame, require_divergence: bool = False) -> dict:
        """BB+RSI 극단값 진입 신호 확인.

        Long 진입 조건:
            1. 가격 < Lower BB
            2. RSI < 과매도 기준 (25)
            3. (선택) Bullish Divergence 확인
            4. BB Width가 충분히 넓어야 함 (Squeeze 아님)

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame.
        require_divergence : bool
            True면 Bullish Divergence가 확인된 경우에만 LONG 신호를 반환한다
            (BUG-11 FIX: 이전에는 divergence 여부가 reason 문자열에만 노출되고
            실제 진입 필터로는 전혀 사용되지 않았다). 기본값 False로 기존 동작
            호환성을 유지하면서, 호출자가 명시적으로 더 엄격한 필터를 선택할 수 있게 함.

        Returns
        -------
        dict
            {
                "signal": "LONG" | "NONE",
                "price": int,
                "rsi": float,
                "bb_lower": float,
                "divergence": bool (BUG-11 FIX: 실제 계산된 divergence 결과 노출),
                "reason": str
            }
        """
        min_len = max(self.bb_length, self.rsi_length) + 1
        if len(df) < min_len:
            return {"signal": "NONE", "price": 0, "rsi": 0.0,
                    "bb_lower": 0.0, "divergence": False, "reason": "데이터 부족"}

        df = self.compute_bb(df, self.bb_length, self.bb_mult)
        df["rsi"] = self.compute_rsi(df, self.rsi_length)

        current_close = df["close"].iloc[-1]
        current_rsi = df["rsi"].iloc[-1]
        bb_lower = df["bb_lower"].iloc[-1]
        bb_width = df["bb_width"].iloc[-1]

        if pd.isna(current_rsi) or pd.isna(bb_lower) or pd.isna(bb_width):
            return {"signal": "NONE", "price": 0, "rsi": 0.0,
                    "bb_lower": 0.0, "divergence": False, "reason": "지표 계산 불가 (NaN)"}

        # BB Width 체크: 너무 좁으면 Squeeze 상태 → 진입 금지
        # BB Width가 20일 평균의 70% 미만이면 변동성 수축
        bb_width_mean = df["bb_width"].rolling(window=20).mean().iloc[-1]
        if pd.notna(bb_width_mean) and bb_width < bb_width_mean * 0.7:
            return {
                "signal": "NONE",
                "price": int(current_close),
                "rsi": float(current_rsi),
                "bb_lower": float(bb_lower),
                "divergence": False,
                "reason": f"BB Width 수축 (width={bb_width:.4f}, mean={bb_width_mean:.4f})",
            }

        # Long 진입 조건
        if current_close < bb_lower and current_rsi < self.rsi_oversold:
            # BUG-11 FIX: Bullish Divergence 결과를 실제 리턴값에 노출하고,
            # require_divergence=True일 때는 실제 진입 필터로 사용한다.
            # (이전에는 reason 문자열에만 붙을 뿐 실제 필터 역할을 하지 않았음)
            has_divergence = self._detect_bullish_divergence(df)

            if require_divergence and not has_divergence:
                return {
                    "signal": "NONE",
                    "price": int(current_close),
                    "rsi": float(current_rsi),
                    "bb_lower": float(bb_lower),
                    "divergence": False,
                    "reason": "과매도 극단값이지만 Bullish Divergence 미확인 (require_divergence=True)",
                }

            div_note = " + Bullish Divergence" if has_divergence else ""

            reason = (
                f"과매도 극단값: 가격({current_close:,.0f}) < BB Lower({bb_lower:,.0f}), "
                f"RSI({current_rsi:.1f}) < {self.rsi_oversold}{div_note}"
            )
            return {
                "signal": "LONG",
                "price": int(current_close),
                "rsi": float(current_rsi),
                "bb_lower": float(bb_lower),
                "divergence": bool(has_divergence),
                "reason": reason,
            }

        return {
            "signal": "NONE",
            "price": int(current_close),
            "rsi": float(current_rsi),
            "bb_lower": float(bb_lower),
            "divergence": False,
            "reason": "진입 조건 미충족",
        }

    def check_exit(self, df: pd.DataFrame, entry_price: float) -> dict:
        """BB+RSI 평균회귀 청산 신호.

        청산 조건:
            - 가격이 Middle BB 도달
            - RSI > 50 (중립 회복)

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame.
        entry_price : float
            진입 가격.

        Returns
        -------
        dict
            {
                "signal": "EXIT" | "HOLD",
                "price": int,
                "reason": str
            }
        """
        min_len = max(self.bb_length, self.rsi_length) + 1
        if len(df) < min_len:
            return {"signal": "HOLD", "price": 0, "reason": "데이터 부족"}

        df = self.compute_bb(df, self.bb_length, self.bb_mult)
        df["rsi"] = self.compute_rsi(df, self.rsi_length)

        current_close = df["close"].iloc[-1]
        current_rsi = df["rsi"].iloc[-1]
        bb_middle = df["bb_middle"].iloc[-1]

        if pd.isna(current_rsi) or pd.isna(bb_middle):
            return {"signal": "HOLD", "price": 0, "reason": "지표 NaN"}

        # Middle BB 도달
        if current_close >= bb_middle:
            return {
                "signal": "EXIT",
                "price": int(current_close),
                "reason": f"Middle BB 도달 (close={current_close:,.0f}, middle={bb_middle:,.0f})",
            }

        # RSI 50 이상 회복
        if current_rsi >= 50:
            return {
                "signal": "EXIT",
                "price": int(current_close),
                "reason": f"RSI 중립 회복 (RSI={current_rsi:.1f})",
            }

        return {
            "signal": "HOLD",
            "price": int(current_close),
            "reason": "청산 조건 미충족",
        }

    # ------------------------------------------------------------------
    # 백테스트
    # ------------------------------------------------------------------
    def run_backtest(self, df_hourly: Optional[pd.DataFrame] = None) -> dict:
        """BB+RSI 평균회귀 백테스트.

        Parameters
        ----------
        df_hourly : pd.DataFrame, optional
            1시간봉 데이터. None이면 DB에서 로드.

        Returns
        -------
        dict
            백테스트 통계.
        """
        if df_hourly is None:
            df_hourly = self.load_data("1h")

        df = df_hourly.copy()
        df = self.compute_bb(df, self.bb_length, self.bb_mult)
        df["rsi"] = self.compute_rsi(df, self.rsi_length)
        df["bb_width_mean"] = df["bb_width"].rolling(window=20).mean()

        trades = []
        in_position = False
        entry_price = 0.0
        entry_date = None

        min_idx = max(self.bb_length, self.rsi_length, 20) + 1
        for i in range(min_idx, len(df)):
            close = df["close"].iloc[i]
            rsi_val = df["rsi"].iloc[i]
            bb_lower = df["bb_lower"].iloc[i]
            bb_middle = df["bb_middle"].iloc[i]
            bb_width = df["bb_width"].iloc[i]
            bb_width_mean = df["bb_width_mean"].iloc[i]

            if pd.isna(rsi_val) or pd.isna(bb_lower) or pd.isna(bb_width_mean):
                continue

            # Squeeze 필터: BB Width가 평균의 70% 미만이면 스킵
            if bb_width < bb_width_mean * 0.7:
                if in_position:
                    # Squeeze 진입 시 강제 청산
                    trades.append({
                        "date": str(df.index[i]),
                        "type": "EXIT",
                        "price": close,
                        "reason": "Squeeze 강제 청산",
                    })
                    in_position = False
                continue

            if not in_position:
                # Long 진입: 가격 < Lower BB + RSI < 25
                if close < bb_lower and rsi_val < self.rsi_oversold:
                    in_position = True
                    entry_price = close
                    entry_date = df.index[i]
                    trades.append({
                        "date": str(entry_date),
                        "type": "ENTRY",
                        "price": close,
                        "rsi": rsi_val,
                    })
            else:
                # 청산: Middle BB 도달 또는 RSI > 50
                if close >= bb_middle or rsi_val >= 50:
                    exit_reason = "Middle BB 도달" if close >= bb_middle else "RSI 회복"
                    trades.append({
                        "date": str(df.index[i]),
                        "type": "EXIT",
                        "price": close,
                        "reason": exit_reason,
                    })
                    in_position = False
                    entry_price = 0.0

        # --- 결과 집계 ---
        entry_trades = [t for t in trades if t["type"] == "ENTRY"]
        exit_trades = [t for t in trades if t["type"] == "EXIT"]

        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        paired = []

        n_pairs = min(len(entry_trades), len(exit_trades))
        for j in range(n_pairs):
            ret = (exit_trades[j]["price"] - entry_trades[j]["price"]) / entry_trades[j]["price"] * 100
            if ret > 0:
                wins += 1
                gross_profit += ret
            else:
                losses += 1
                gross_loss += abs(ret)
            paired.append({
                "entry_date": entry_trades[j]["date"],
                "exit_date": exit_trades[j]["date"],
                "entry_price": entry_trades[j]["price"],
                "exit_price": exit_trades[j]["price"],
                "return_pct": round(ret, 2),
                "reason": exit_trades[j].get("reason", ""),
            })

        total = wins + losses
        win_rate = wins / total * 100 if total > 0 else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        total_return = sum(p["return_pct"] for p in paired)

        return {
            "total_return": round(total_return, 2),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "total_trades": total,
            "trades": paired,
        }


# ---------------------------------------------------------------------------
# 데모 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    DB_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "upbit.db",
    )

    print("=" * 60)
    print("  🔄 BB + RSI Extreme Mean Reversion — 데모")
    print("=" * 60)

    ticker = "KRW-BTC"
    strategy = BBRSIReversalStrategy(DB_PATH, ticker)

    try:
        print(f"\n📊 {ticker} 1시간봉 데이터 로드 중...")
        df = strategy.load_data("1h")
        print(f"   데이터: {len(df)}행, {df.index[0]} ~ {df.index[-1]}")

        # 지표 계산
        df = strategy.compute_bb(df)
        rsi = strategy.compute_rsi(df)
        print(f"   현재가: {df['close'].iloc[-1]:,.0f} KRW")
        print(f"   BB Upper: {df['bb_upper'].iloc[-1]:,.0f}")
        print(f"   BB Middle: {df['bb_middle'].iloc[-1]:,.0f}")
        print(f"   BB Lower: {df['bb_lower'].iloc[-1]:,.0f}")
        print(f"   BB Width: {df['bb_width'].iloc[-1]:.4f}")
        print(f"   RSI(13): {rsi.iloc[-1]:.1f}")

        # 진입 신호
        entry = strategy.check_entry(df)
        print(f"\n🎯 진입 신호: {entry}")

        # 청산 신호
        exit_ = strategy.check_exit(df, entry["price"])
        print(f"🚪 청산 신호: {exit_}")

        # 백테스트
        print(f"\n📈 백테스트 실행 중...")
        result = strategy.run_backtest(df)
        print(f"   총 수익률: {result['total_return']:+.2f}%")
        print(f"   승률: {result['win_rate']:.1f}%")
        print(f"   Profit Factor: {result['profit_factor']:.2f}")
        print(f"   총 거래: {result['total_trades']}회")
        if result["trades"]:
            print(f"\n   최근 5개 거래:")
            for t in result["trades"][-5:]:
                print(f"     {t['entry_date'][:19]} → {t['exit_date'][:19]}: "
                      f"{t['return_pct']:+.2f}% ({t['reason']})")

    except Exception as e:
        print(f"\n❌ 오류: {e}")
        import traceback
        traceback.print_exc()

    print("\n✅ BB+RSI Reversal Strategy 데모 완료")