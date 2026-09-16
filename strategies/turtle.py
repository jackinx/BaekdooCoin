"""
터틀 트레이딩 시스템 (Turtle Trading System)
===========================================

Richard Dennis & Bill Eckhardt의 전설적인 터틀 실험에 기반한
추세추종 전략. 55일 Donchian Channel (System 2)을 사용합니다.

핵심 원칙:
    - 진입: 55일 최고가 돌파 (System 2)
    - 청산: 20일 최저가 하향 돌파
    - 손절: 진입가 - 2×ATR
    - 포지션 사이징: 계좌의 1% / ATR
    - 피라미딩: 최대 2 Unit (현물 제한)

참조:
    - strategy_research.md — Turtle Trading System 상세 스펙
    - Gate Research Institute (2025): GT/USDT 연환산 +62.71%
    - Altrady Blog (2026): BTC 8년 전 구간 수익 검증
"""

import logging
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class TurtleStrategy:
    """터틀 트레이딩 시스템 (System 2, 55일 Donchian Channel).

    업비트 현물 전용으로 최적화: 롱온리, 피라미딩 2 Unit 제한,
    ATR 기반 리스크 사이징.
    """

    def __init__(self, db_path: str, ticker: str):
        """
        Parameters
        ----------
        db_path : str
            SQLite 데이터베이스 파일 경로.
        ticker : str
            업비트 티커 (예: "KRW-BTC").
        """
        self.db_path = db_path
        self.ticker = ticker
        logger.info("터틀 전략 초기화: ticker=%s", ticker)

    # ------------------------------------------------------------------
    # 데이터 로딩
    # ------------------------------------------------------------------
    def load_daily_data(self) -> pd.DataFrame:
        """1일봉 데이터를 DB에서 로드하여 DataFrame으로 반환.

        Returns
        -------
        pd.DataFrame
            timestamp, open, high, low, close, volume, volume_krw 컬럼 포함.
            timestamp는 datetime64로 변환되고 인덱스로 설정됨.
        """
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT timestamp, open, high, low, close, volume, volume_krw
            FROM ohlcv_1d
            WHERE ticker = ?
            ORDER BY timestamp ASC
        """
        df = pd.read_sql_query(query, conn, params=(self.ticker,))
        conn.close()

        if df.empty:
            raise ValueError(f"1일봉 데이터가 없습니다: ticker={self.ticker}")

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    @staticmethod
    def get_donchian(df: pd.DataFrame, period: int) -> pd.DataFrame:
        """Donchian Channel (최근 N일 최고가/최저가) 계산.

        Parameters
        ----------
        df : pd.DataFrame
            high, low 컬럼이 포함된 OHLCV DataFrame.
        period : int
            기간 (일수).

        Returns
        -------
        pd.DataFrame
            'dc_high', 'dc_low', 'dc_mid' 컬럼이 추가된 원본 DataFrame.
        """
        df = df.copy()
        df["dc_high"] = df["high"].rolling(window=period).max()
        df["dc_low"] = df["low"].rolling(window=period).min()
        df["dc_mid"] = (df["dc_high"] + df["dc_low"]) / 2
        return df

    @staticmethod
    def get_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """Average True Range (ATR) 계산.

        Parameters
        ----------
        df : pd.DataFrame
            high, low, close 컬럼이 포함된 DataFrame.
        period : int
            ATR 계산 기간 (기본값: 20).

        Returns
        -------
        pd.Series
            ATR 값 시리즈.
        """
        df = df.copy()
        prev_close = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr

    # ------------------------------------------------------------------
    # 진입/청산 신호
    # ------------------------------------------------------------------
    def check_entry(self, df: pd.DataFrame) -> dict:
        """55일 Donchian Channel 상향 돌파 진입 신호 확인.

        System 2 진입 규칙:
            - 현재 종가가 55일 최고가를 상향 돌파 → LONG 신호
            - 아니면 NONE

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame (충분한 히스토리 필요, 최소 56행).

        Returns
        -------
        dict
            {
                "signal": "LONG" | "NONE",
                "price": int (진입가),
                "atr": float (현재 ATR),
                "reason": str (신호 사유)
            }
        """
        if len(df) < 55:
            return {"signal": "NONE", "price": 0, "atr": 0.0,
                    "reason": "데이터 부족 (< 55봉)"}

        df = self.get_donchian(df, period=55)
        atr_series = self.get_atr(df, period=20)

        # BUG-1/BUG-2 FIX: dc_high는 당일 봉의 high를 포함한 rolling max이므로
        # close > dc_high(당일)은 수학적으로 항상 거짓이다 (close <= high <= dc_high).
        # 돌파 판정은 반드시 '직전까지의' 55일 고점(당일 제외)과 비교해야 한다.
        prior_high_55 = df["dc_high"].shift(1)

        # 최신 캔들
        prev_close = df["close"].iloc[-2]
        prev_high_55 = prior_high_55.iloc[-2]
        current_close = df["close"].iloc[-1]
        current_high_55 = prior_high_55.iloc[-1]
        current_atr = atr_series.iloc[-1]

        # 55일 최고가(당일 제외) 상향 돌파: 이전 종가 ≤ 55일 고가, 현재 종가 > 55일 고가
        if prev_close <= prev_high_55 and current_close > current_high_55:
            return {
                "signal": "LONG",
                "price": int(current_close),
                "atr": float(current_atr),
                "reason": f"55일 최고가 돌파 (dc_high={current_high_55:,.0f}, close={current_close:,.0f})",
            }
        return {"signal": "NONE", "price": int(current_close), "atr": float(current_atr),
                "reason": "55일 최고가 미돌파"}

    def check_exit(self, df: pd.DataFrame, entry_price: float) -> dict:
        """20일 최저가 하향 돌파 청산 신호 확인.

        System 2 청산 규칙:
            - 현재 종가가 20일 최저가를 하향 돌파 → EXIT
            - 아니면 HOLD

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV DataFrame.
        entry_price : float
            진입 가격 (손절 가격 계산용).

        Returns
        -------
        dict
            {
                "signal": "EXIT" | "HOLD",
                "price": int (청산가),
                "reason": str (청산 사유)
            }
        """
        if len(df) < 20:
            return {"signal": "HOLD", "price": 0, "reason": "데이터 부족"}

        df = self.get_donchian(df, period=20)
        atr_series = self.get_atr(df, period=20)

        # BUG-3 FIX: dc_low(20일)도 당일 low를 포함하므로 당일 제외 버전을 사용해야
        # close < dc_low(당일)이 항상 거짓이 되는 문제를 피할 수 있다.
        current_close = df["close"].iloc[-1]
        current_low_20 = df["dc_low"].shift(1).iloc[-1]
        current_atr = atr_series.iloc[-1]

        # 20일 최저가(당일 제외) 하향 돌파
        if current_close < current_low_20:
            return {
                "signal": "EXIT",
                "price": int(current_close),
                "reason": f"20일 최저가 하향 돌파 (dc_low={current_low_20:,.0f}, close={current_close:,.0f})",
            }

        # 2×ATR 손절 체크
        stop_loss = entry_price - 2 * current_atr
        if current_close <= stop_loss:
            return {
                "signal": "EXIT",
                "price": int(current_close),
                "reason": f"2×ATR 손절 도달 (entry={entry_price:,.0f}, ATR={current_atr:,.0f}, stop={stop_loss:,.0f})",
            }

        return {"signal": "HOLD", "price": int(current_close),
                "reason": "청산 조건 미충족"}

    # ------------------------------------------------------------------
    # 포지션 사이징
    # ------------------------------------------------------------------
    def calculate_position_size(
        self, equity_krw: float, atr: float, current_price: float
    ) -> int:
        """ATR 기반 포지션 사이즈 계산 (업비트 현물).

        원화 현물 버전:
            Unit = (계좌잔고 × 1%) ÷ ATR
            매수금액 = Unit × 현재가격 (KRW)

        Parameters
        ----------
        equity_krw : float
            계좌 잔고 (KRW).
        atr : float
            현재 ATR 값.
        current_price : float
            현재 가격 (KRW).

        Returns
        -------
        int
            매수 금액 (KRW). 최소 5,000 KRW.
        """
        if atr <= 0 or current_price <= 0:
            return 0

        # 1 Unit = 계좌잔고의 1% ÷ (ATR / 현재가격)
        # → 계좌의 1% 리스크를 ATR 기준으로 환산
        risk_krw = equity_krw * 0.01
        # ATR / price = 변동성 비율
        volatility_ratio = atr / current_price
        if volatility_ratio <= 0:
            return 0

        # 1 Unit 매수금액 = 리스크금액 / 변동성비율
        unit_krw = risk_krw / volatility_ratio

        # 업비트 최소 주문금액 + 계좌 잔고 상한 (BUG-5 FIX + SEC-AUDIT)
        # 극도로 낮은 변동성(atr/price가 매우 작을 때)에서 unit_krw가
        # equity_krw를 초과할 수 있으므로 상한을 설정한다.
        result = max(int(unit_krw), 5_000)
        result = min(result, int(equity_krw))
        return result

    # ------------------------------------------------------------------
    # 백테스트
    # ------------------------------------------------------------------
    def run_backtest(self, df_daily: Optional[pd.DataFrame] = None) -> dict:
        """터틀 System 2 백테스트 실행.

        Parameters
        ----------
        df_daily : pd.DataFrame, optional
            1일봉 데이터. None이면 DB에서 로드.

        Returns
        -------
        dict
            {
                "total_return": float (총 수익률),
                "win_rate": float (승률),
                "profit_factor": float (Profit Factor),
                "total_trades": int,
                "trades": list[dict] (개별 거래 내역)
            }
        """
        if df_daily is None:
            df_daily = self.load_daily_data()

        df = df_daily.copy()
        df = self.get_donchian(df, period=55)
        df = self.get_donchian(df, period=20)  # dc_high_20, dc_low_20
        # dc_20 컬럼명 충돌 방지 — 이미 55로 계산된 dc_high, dc_low가 있음
        # 20일 Donchian은 별도 컬럼으로 관리
        df["dc_high_20"] = df["high"].rolling(window=20).max()
        df["dc_low_20"] = df["low"].rolling(window=20).min()
        df["atr"] = self.get_atr(df, period=20)

        # BUG-1/BUG-3 FIX: 당일 봉을 제외한 '직전까지'의 Donchian 채널을 사용.
        # 그렇지 않으면 close > dc_high(당일 포함) 또는 close < dc_low(당일 포함)는
        # 항상 거짓이 되어 진입/청산이 영원히 발생하지 않는다.
        df["dc_high_prior"] = df["dc_high"].shift(1)
        df["dc_low_20_prior"] = df["dc_low_20"].shift(1)

        trades = []
        in_position = False
        entry_price = 0.0
        entry_atr = 0.0
        entry_date = None
        units = 0  # 현재 피라미딩 유닛 수
        last_add_price = 0.0  # 마지막 추가 진입 가격

        for i in range(55, len(df)):  # 55일 초기 기간 이후부터
            close = df["close"].iloc[i]
            prev_close = df["close"].iloc[i - 1]
            atr = df["atr"].iloc[i]
            # BUG-1/BUG-3 FIX: 당일 봉을 제외한(shift(1)) Donchian 채널 사용
            high_55 = df["dc_high_prior"].iloc[i]
            prev_high_55 = df["dc_high_prior"].iloc[i - 1]
            low_20 = df["dc_low_20_prior"].iloc[i]
            date = df.index[i]

            if pd.isna(high_55) or pd.isna(low_20) or pd.isna(atr) or atr <= 0:
                continue

            if not in_position:
                # --- 진입 신호: 55일 고점(당일 제외) 돌파 ---
                if prev_close <= prev_high_55 and close > high_55:
                    in_position = True
                    entry_price = close
                    entry_atr = atr
                    entry_date = date
                    units = 1
                    last_add_price = close
                    trades.append({
                        "date": str(date),
                        "type": "ENTRY",
                        "price": close,
                        "atr": atr,
                        "units": units,
                    })
            else:
                # --- 피라미딩: 0.5×ATR 상승 시 추가 진입 ---
                if units < 2 and close >= last_add_price + 0.5 * entry_atr:
                    units += 1
                    last_add_price = close
                    trades.append({
                        "date": str(date),
                        "type": "PYRAMID",
                        "price": close,
                        "atr": atr,
                        "units": units,
                    })

                # --- 청산: 20일 저가 하향 돌파 또는 2×ATR 손절 ---
                stop_loss = entry_price - 2 * entry_atr
                if close < low_20 or close <= stop_loss:
                    exit_reason = (
                        "20일 저가 하향 돌파" if close < low_20 else "2×ATR 손절"
                    )
                    trades.append({
                        "date": str(date),
                        "type": "EXIT",
                        "price": close,
                        "atr": atr,
                        "units": units,
                        "reason": exit_reason,
                    })
                    in_position = False
                    entry_price = 0.0
                    entry_atr = 0.0
                    units = 0
                    last_add_price = 0.0

        # --- 백테스트 결과 집계 ---
        entry_trades = [t for t in trades if t["type"] == "ENTRY"]
        exit_trades = [t for t in trades if t["type"] == "EXIT"]

        # 페어링: ENTRY와 EXIT를 짝지어서 수익 계산
        # (백테스트에서 ENTRY-EXIT는 순차적으로 발생)
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        paired_trades = []

        e_idx = 0
        x_idx = 0
        while e_idx < len(entry_trades) and x_idx < len(exit_trades):
            entry = entry_trades[e_idx]
            exit_ = exit_trades[x_idx]
            return_pct = (exit_["price"] - entry["price"]) / entry["price"] * 100
            if return_pct > 0:
                wins += 1
                gross_profit += return_pct
            else:
                losses += 1
                gross_loss += abs(return_pct)
            paired_trades.append({
                "entry_date": entry["date"],
                "exit_date": exit_["date"],
                "entry_price": entry["price"],
                "exit_price": exit_["price"],
                "return_pct": round(return_pct, 2),
                "reason": exit_.get("reason", ""),
            })
            e_idx += 1
            x_idx += 1

        total_trades = wins + losses
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0.0
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        # 총 수익률 (단리 합산)
        total_return = sum(t["return_pct"] for t in paired_trades)

        return {
            "total_return": round(total_return, 2),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "total_trades": total_trades,
            "trades": paired_trades,
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
    print("  🐢 Turtle Trading System (System 2) — 데모")
    print("=" * 60)

    #BTC 백테스트
    ticker = "KRW-BTC"
    strategy = TurtleStrategy(DB_PATH, ticker)

    try:
        print(f"\n📊 {ticker} 1일봉 데이터 로드 중...")
        df = strategy.load_daily_data()
        print(f"   데이터: {len(df)}행, {df.index[0]} ~ {df.index[-1]}")

        # 지표 계산
        df_with_dc = strategy.get_donchian(df, period=55)
        atr = strategy.get_atr(df, period=20)
        print(f"   현재가: {df['close'].iloc[-1]:,.0f} KRW")
        print(f"   55일 DC High: {df_with_dc['dc_high'].iloc[-1]:,.0f} KRW")
        print(f"   55일 DC Low: {df_with_dc['dc_low'].iloc[-1]:,.0f} KRW")
        print(f"   ATR(20): {atr.iloc[-1]:,.0f} KRW")
        print(f"   ATR/Price: {atr.iloc[-1]/df['close'].iloc[-1]*100:.2f}%")

        # 진입 신호
        entry = strategy.check_entry(df)
        print(f"\n🎯 진입 신호: {entry}")

        # 포지션 사이즈 (예: 10,000,000 KRW)
        equity = 10_000_000
        if entry["atr"] > 0:
            pos_size = strategy.calculate_position_size(
                equity, entry["atr"], entry["price"]
            )
            print(f"💰 포지션 사이즈 (잔고 ₩{equity:,.0f}): ₩{pos_size:,.0f}")

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
                print(f"     {t['entry_date'][:10]} → {t['exit_date'][:10]}: "
                      f"{t['return_pct']:+.2f}% ({t['reason']})")

    except Exception as e:
        print(f"\n❌ 오류: {e}")
        import traceback
        traceback.print_exc()

    print("\n✅ Turtle Strategy 데모 완료")