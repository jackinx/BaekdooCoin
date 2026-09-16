#!/usr/bin/env python3
"""백두코인봇 dry-run — 주문 생성만 하고 실제 실행은 안 함"""
import sys, time, logging
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('dryrun')

from config import Config
from strategies.turtle import TurtleStrategy

DB = "data/upbit_v2.db"
TICKER = "KRW-BTC"

cfg = Config.load()
print("="*60)
print(" 백두코인봇 Dry-Run 테스트")
print(f" 대상: {TICKER} (Turtle 전략 1D)")
print(f" 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*60)

# 1. 전략 신호 확인
turtle = TurtleStrategy(DB, TICKER)
df = turtle.load_daily_data()
entry = turtle.check_entry(df)

atr_val = turtle.get_atr(df, period=20).iloc[-1]
last_close = df["close"].iloc[-1]

print(f"\n📊 {TICKER} 현재 상태")
print(f"   데이터: {len(df)}일 ({df.index[0].date()} ~ {df.index[-1].date()})")
print(f"   현재가: ₩{last_close:,.0f}")
print(f"   ATR(20): ₩{atr_val:,.0f} ({atr_val/last_close*100:.1f}%)")
print(f"   진입 신호: {entry}")

# 2. 포지션 사이즈 계산
equity = 300000  # 실제 잔고
if entry["signal"] == "LONG" and entry["atr"] > 0:
    pos_size = turtle.calculate_position_size(equity, entry["atr"], entry["price"])
    print(f"\n💰 포지션 사이즈")
    print(f"   계좌잔고: ₩{equity:,}")
    print(f"   진입가격: ₩{entry['price']:,}")
    print(f"   1% 리스크: ₩{equity * 0.01:,.0f}")
    print(f"   투입금액: ₩{pos_size:,} ({pos_size/equity*100:.1f}%)")
    
    # 주문 시뮬레이션
    print(f"\n📝 가상 주문서")
    print(f"   ┌─────────────────────────────────────┐")
    print(f"   │ 매수: {TICKER}                          │")
    print(f"   │ 전략: Turtle (55D Donchian)          │")
    print(f"   │ 방식: 시장가 매수                      │")
    print(f"   │ 금액: ₩{pos_size:,}                     │")
    print(f"   │ 주문ID: baekdoocoin_{TICKER}_dryrun   │")
    print(f"   └─────────────────────────────────────┘")
else:
    print(f"\n⏸️  진입 신호 없음 — 대기")

# 3. 업비트 연결 확인
print(f"\n🔌 업비트 연결 확인 중...")
try:
    from upbit_client import UpbitClient
    u = UpbitClient(cfg.upbit_access_key, cfg.upbit_secret_key)
    if u.check_connection():
        krw = u.get_krw_balance()
        print(f"   ✅ 연결 성공 — 원화 잔고: ₩{krw:,.0f}")
        # 최소주문 가능 여부
        if krw >= 5000:
            print(f"   ✅ 최소주문금액(₩5,000) 충족")
        else:
            print(f"   ❌ 최소주문금액 미달 (₩{krw:,.0f} < ₩5,000)")
            print(f"   → 입금 필요합니다")
    else:
        print(f"   ❌ 연결 실패")
except Exception as e:
    print(f"   ❌ 오류: {e}")

print(f"\n{'='*60}")
print(" Dry-Run 완료 — 실제 주문은 실행되지 않았습니다")
print(f"{'='*60}")