"""
스퀴즈 모멘텀 전략 (Squeeze Momentum Indicator)
==============================================

John Carter "TTM Squeeze" → LazyBear가 TradingView Pine Script로 포팅한
변동성 수축/확장 기반 모멘텀 전략.

핵심 아이디어:
    - 볼린저 밴드가 켈트너 채널 안에 들어오면 변동성 수축 (Squeeze)
    - Squeeze 해제 순간에 모멘텀 방향으로 진입
    - Multi-TF: 4H Squeeze 감지 → 1H 진입 신호

참조:
    - strategy_research.md — Squeeze Momentum 상세 스펙
    - TradingView: LazyBear script `nqQ1DT5a`
    - BTC 30m 백테스트 (2024-2025): PF 2.01, 승률 47.8%
"""

import logging
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SqueezeMomentumStrategy:
    """스퀴즈 모멘텀 전략 (LazyBear / John Carter).

    볼린저 밴드와 켈트너 채널의 상대적 위치로 변동성 수축을 감지하고,
    Squeeze 해제 시 모멘텀 방향으로 진입합니다.
    """

    def __init__(
        self,
        db_path: str,
        ticker: str,
        bb_length: int = 20,
        bb_mult: float = 2.0,
        kc_length: int = 20,
        kc_mult: float = 1.5,
    ):
        """
        Parameters
        ----------
        db_path : str
            SQLite 데이터베이스 파일 경로.
        ticker : str
            업비트 티커 (예: "KRW-BTC").
        bb_length : int
            볼린저 밴드 기간 (기본값: 20).
        bb_mult : float
            볼린저 밴드 승수 (기본값: 2.0).
        kc_length : int
            켈트너 채널 기간 (기본값: 20).
        kc_mult : float
            켈트너 채널 ATR 승수 (기본값: 1.5).
        """
        self.db_path = db_path
        self.ticker = ticker
        self.bb_length = bb_length
        self.bb_mult = bb_mult
        self.kc_length = kc_length
        self.kc_mult = kc_mult
        logger.info(
            "스퀴즈 모멘텀 초기화: ticker=%s, BB(%d,%.1f), KC(%d,%.1f)",
            ticker, bb_length, bb_mult, kc_length, kc_mult,
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
    def compute_bb(df: pd.DataFrame, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
        """볼린저 밴드 (Bollinger Bands) 계산.

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
    def compute_kc(df: pd.DataFrame, length: int = 20, mult: float = 1.5) -> pd.DataFrame:
        """켈트너 채널 (Keltner Channels) 계산 — ATR 기반.

        Parameters
        ----------
        df : pd.DataFrame
            high, low, close 컬럼이 포함된 DataFrame.
        length : int
            평균 기간.
        mult : float
            ATR 승수.

        Returns
        -------
        pd.DataFrame
            'kc_middle', 'kc_upper', 'kc_lower' 컬럼 추가.
        """
        df = df.copy()

        # True Range
        prev_close = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Average True Range
        atr = true_range.rolling(window=length).mean()

        # KC Middle = EMA of close
        df["kc_middle"] = df["close"].ewm(span=length, adjust=False).mean()
        df["kc_upper"] = df["kc_middle"] + mult * atr
        df["kc_lower"] = df["kc_middle"] - mult * atr
        return df

    @staticmethod
    def compute_momentum(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
        """선형 회귀 모멘텀 (John Carter 방식) 계산.

        John Carter의 오리지널 모멘텀 계산:
            - 최근 N개 종가에 대한 선형 회귀 기울기
            - 델타 = 선형 회귀의 마지막 값과 첫 값의 차이

        Parameters
        ----------
        df : pd.DataFrame
            close 컬럼이 포함된 DataFrame.
        length : int
            회귀 기간 (기본값: 20).

        Returns
        -------
        pd.DataFrame
            'momentum', 'momentum_direction' 컬럼 추가.
        """
        df = df.copy()

        # 선형 회귀 기반 모멘텀: John Carter 방식
        # X = 0, 1, ..., length-1 (바 인덱스)
        # Y = close[-length:]
        # Linear Regression Delta = slope * (length - 1)
        #
        # BUG-8 FIX: 이전 구현은 Python for-루프 안에서 매 row마다 점점
        # 커지는 슬라이스를 재계산하여 O(n²)였다. 폐쇄형 OLS 기울기
        # = Cov(x,y) / Var(x) 를 rolling.apply(raw=True)로 계산하면
        # 동일한 결과를 훨씬 빠르게 O(n)에 얻는다.
        x_idx = np.arange(length)
        x_mean = x_idx.mean()
        x_var_sum = float(((x_idx - x_mean) ** 2).sum())  # 상수 (윈도우 고정 길이)

        def _ols_slope(y: np.ndarray) -> float:
            y_mean = y.mean()
            cov_sum = float(np.sum((x_idx - x_mean) * (y - y_mean)))
            return cov_sum / x_var_sum

        slope = df["close"].rolling(window=length).apply(_ols_slope, raw=True)
        df["momentum"] = slope * (length - 1)
        # BUG-7 FIX 준비: 지나치게 노이즈한 raw momentum 대신
        # 청산 판단에 쓸 수 있도록 EMA로 스무싱된 버전도 함께 생성한다.
        df["momentum_smooth"] = df["momentum"].ewm(span=3, adjust=False).mean()
        df["momentum_direction"] = np.where(
            df["momentum"] > 0, 1, np.where(df["momentum"] < 0, -1, 0)
        )
        return df

    # ------------------------------------------------------------------
    # Squeeze 감지
    # ------------------------------------------------------------------
    def is_squeeze(self, df: pd.DataFrame) -> pd.Series:
        """Squeeze 상태 판단: BB가 KC 안에 있는지 확인.

        Squeeze 조건:
            BB 하단 > KC 하단 AND BB 상단 < KC 상단
            → 변동성 수축 상태

        Parameters
        ----------
        df : pd.DataFrame
            BB와 KC가 계산된 DataFrame.

        Returns
        -------
        pd.Series
            True = Squeeze 상태, False = Squeeze 해제.
        """
        return (df["bb_lower"] > df["kc_lower"]) & (df["bb_upper"] < df["kc_upper"])

    # ------------------------------------------------------------------
    # 진입/청산 신호
    # ------------------------------------------------------------------
    def check_entry(self, df: pd.DataFrame) -> dict:
        """Squeeze 해제 진입 신호 확인.

        Squeeze가 해제된 첫 번째 봉에서 모멘텀 방향으로 진입:
            - Squeeze 해제 + 모멘텀 양수 → LONG
            - Squeeze 해제 + 모멘텀 음수 → SHORT (현물은 무시)
            - Squeeze 중이거나 해제 신호 없음 → NONE

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame.

        Returns
        -------
        dict
            {
                "signal": "LONG" | "SHORT" | "NONE",
                "price": int,
                "momentum": float,
                "reason": str
            }
        """
        min_len = max(self.bb_length, self.kc_length) + 1
        if len(df) < min_len:
            return {"signal": "NONE", "price": 0, "momentum": 0.0,
                    "reason": "데이터 부족"}

        df = self.compute_bb(df, self.bb_length, self.bb_mult)
        df = self.compute_kc(df, self.kc_length, self.kc_mult)
        df = self.compute_momentum(df, self.bb_length)

        squeeze = self.is_squeeze(df)

        # 이전 봉 Squeeze 상태, 현재 봉 Squeeze 상태
        prev_squeeze = squeeze.iloc[-2] if len(squeeze) >= 2 else False
        curr_squeeze = squeeze.iloc[-1]
        curr_momentum = df["momentum"].iloc[-1]
        curr_close = df["close"].iloc[-1]

        # Squeeze 해제 감지: 이전에 Squeeze였고, 현재는 아님
        squeeze_fired = prev_squeeze and not curr_squeeze

        if squeeze_fired:
            if not pd.isna(curr_momentum) and curr_momentum > 0:
                return {
                    "signal": "LONG",
                    "price": int(curr_close),
                    "momentum": float(curr_momentum),
                    "reason": f"Squeeze 해제 + 모멘텀 상승 (mom={curr_momentum:.4f})",
                }
            elif not pd.isna(curr_momentum) and curr_momentum < 0:
                return {
                    "signal": "SHORT",
                    "price": int(curr_close),
                    "momentum": float(curr_momentum),
                    "reason": f"Squeeze 해제 + 모멘텀 하락 (mom={curr_momentum:.4f})",
                }

        if curr_squeeze:
            return {"signal": "NONE", "price": int(curr_close),
                    "momentum": float(curr_momentum) if not pd.isna(curr_momentum) else 0.0,
                    "reason": "Squeeze 진행 중 (관망)"}

        return {"signal": "NONE", "price": int(curr_close),
                "momentum": float(curr_momentum) if not pd.isna(curr_momentum) else 0.0,
                "reason": "Squeeze 해제 신호 없음"}

    def check_exit(self, df: pd.DataFrame) -> dict:
        """모멘텀 방향 전환 청산 신호.

        청산 조건:
            - 모멘텀이 0 아래로 전환 (롱 포지션 청산)
            - 모멘텀이 0 위로 전환 (숏 포지션 청산)

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame.

        Returns
        -------
        dict
            {
                "signal": "EXIT" | "HOLD",
                "price": int,
                "reason": str
            }
        """
        min_len = max(self.bb_length, self.kc_length) + 1
        if len(df) < min_len:
            return {"signal": "HOLD", "price": 0, "reason": "데이터 부족"}

        df = self.compute_bb(df, self.bb_length, self.bb_mult)
        df = self.compute_kc(df, self.kc_length, self.kc_mult)
        df = self.compute_momentum(df, self.bb_length)

        # BUG-7 FIX: raw 모멘텀(매 봉마다 재계산되는 선형회귀 기울기)은
        # 노이즈가 심해서 부호(방향) 전환이 자주 발생한다 (실측: BTC 1h에서
        # 전체 봉의 약 6.2%가 부호 플립). John Carter의 TTM Squeeze 히스토그램은
        # 색상(모멘텀 부호) 전환을 스무싱된 시리즈에서 판단하므로,
        # EMA로 스무싱된 momentum_smooth의 부호 전환으로 청산을 판단하여
        # 조기 노이즈에 의한 조기 청산을 방지한다.
        curr_close = df["close"].iloc[-1]

        # 실측 결과 EMA(3) 스무싱만으로는 부호 플립 빈도가 충분히 줄어들지 않았다
        # (6.2% → 5.0%). TTM Squeeze의 실제 관례은 히스토그램 색상이 "바뀌어서
        # 유지된다"는 것이므로, 2개 연속 봉 동안 새 부호가 유지될 때만
        # "확정된" 전환으로 인정하여 단일 봉 노이즈에 의한 조기 청산을 막는다.
        mom = df["momentum_smooth"]
        if len(mom) < 3:
            return {"signal": "HOLD", "price": int(curr_close),
                    "reason": "데이터 부족"}
        m2, m1, m0 = mom.iloc[-3], mom.iloc[-2], mom.iloc[-1]

        if pd.notna(m2) and pd.notna(m1) and pd.notna(m0):
            # 양→음 확정 전환: 이전은 양수였고, 최근 2개 봉 연속 음수
            if m2 > 0 and m1 < 0 and m0 < 0:
                return {
                    "signal": "EXIT",
                    "price": int(curr_close),
                    "reason": "모멘텀 방향 전환 (양→음, 2봉 확정)",
                }
            # 음→양 확정 전환
            if m2 < 0 and m1 > 0 and m0 > 0:
                return {
                    "signal": "EXIT",
                    "price": int(curr_close),
                    "reason": "모멘텀 방향 전환 (음→양, 스무싱 확인)",
                }

        return {"signal": "HOLD", "price": int(curr_close),
                "reason": "모멘텀 방향 유지"}

    # ------------------------------------------------------------------
    # 백테스트
    # ------------------------------------------------------------------
    def run_backtest(self, df_hourly: Optional[pd.DataFrame] = None) -> dict:
        """스퀴즈 모멘텀 백테스트 (1시간봉 기준).

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
        df = self.compute_kc(df, self.kc_length, self.kc_mult)
        df = self.compute_momentum(df, self.bb_length)

        squeeze = self.is_squeeze(df)

        trades = []
        in_position = False
        entry_price = 0.0
        entry_date = None

        min_idx = max(self.bb_length, self.kc_length) + 1
        for i in range(min_idx, len(df)):
            if squeeze.iloc[i] and not squeeze.iloc[i - 1]:
                # Squeeze 시작
                pass

            elif not squeeze.iloc[i] and squeeze.iloc[i - 1]:
                # Squeeze 해제 — 진입 신호
                mom = df["momentum"].iloc[i]
                if pd.notna(mom) and mom > 0 and not in_position:
                    in_position = True
                    entry_price = df["close"].iloc[i]
                    entry_date = df.index[i]
                    trades.append({
                        "date": str(entry_date),
                        "type": "ENTRY",
                        "price": entry_price,
                        "momentum": mom,
                    })

            # BUG-7 FIX: check_exit과 동일하게 EMA 스무싱된 momentum_smooth의
            # 부호가 2개 연속 봉 동안 유지될 때만 "확정된" 전환으로 인정하여
            # 단일 봉 노이즈에 의한 조기 청산을 방지한다 (raw slope의 약 6.2%
            # 부호플립 빈도가 PF 0.06의 주원인이었음).
            if in_position and i >= min_idx + 1:
                m2 = df["momentum_smooth"].iloc[i - 2]
                m1 = df["momentum_smooth"].iloc[i - 1]
                m0 = df["momentum_smooth"].iloc[i]
                if pd.notna(m2) and pd.notna(m1) and pd.notna(m0):
                    confirmed_flip_down = m2 > 0 and m1 < 0 and m0 < 0
                    if confirmed_flip_down:
                        exit_price = df["close"].iloc[i]
                        trades.append({
                            "date": str(df.index[i]),
                            "type": "EXIT",
                            "price": exit_price,
                            "momentum": m0,
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
    print("  📦 Squeeze Momentum Strategy (LazyBear) — 데모")
    print("=" * 60)

    ticker = "KRW-BTC"
    strategy = SqueezeMomentumStrategy(DB_PATH, ticker)

    try:
        print(f"\n📊 {ticker} 1시간봉 데이터 로드 중...")
        df = strategy.load_data("1h")
        print(f"   데이터: {len(df)}행, {df.index[0]} ~ {df.index[-1]}")

        # 지표 계산
        df = strategy.compute_bb(df)
        df = strategy.compute_kc(df)
        df = strategy.compute_momentum(df)

        squeeze = strategy.is_squeeze(df)
        squeeze_count = squeeze.sum()
        print(f"   현재가: {df['close'].iloc[-1]:,.0f} KRW")
        print(f"   BB Upper: {df['bb_upper'].iloc[-1]:,.0f}, Middle: {df['bb_middle'].iloc[-1]:,.0f}, Lower: {df['bb_lower'].iloc[-1]:,.0f}")
        print(f"   KC Upper: {df['kc_upper'].iloc[-1]:,.0f}, Middle: {df['kc_middle'].iloc[-1]:,.0f}, Lower: {df['kc_lower'].iloc[-1]:,.0f}")
        print(f"   Squeeze 상태: {squeeze.iloc[-1]}")
        print(f"   과거 Squeeze 비율: {squeeze_count}/{len(df)} ({squeeze_count/len(df)*100:.1f}%)")
        print(f"   모멘텀: {df['momentum'].iloc[-1]:.4f}")

        # 진입 신호
        entry = strategy.check_entry(df)
        print(f"\n🎯 진입 신호: {entry}")

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
                      f"{t['return_pct']:+.2f}%")

    except Exception as e:
        print(f"\n❌ 오류: {e}")
        import traceback
        traceback.print_exc()

    print("\n✅ Squeeze Momentum Strategy 데모 완료")