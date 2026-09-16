import sys, json, time, logging
from pathlib import Path
from datetime import datetime
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.WARNING)

from config import Config
from strategies.turtle import TurtleStrategy

DB = "data/upbit_v2.db"
TICKERS = [
    "KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-ADA",
    "KRW-DOGE", "KRW-AVAX", "KRW-DOT", "KRW-POL", "KRW-LINK",
]
now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
today = datetime.now().strftime("%Y-%m-%d")

# 1. 실시간 가격 수집 (업비트 public API)
import requests
live_prices = {}
try:
    markets = ",".join(TICKERS)
    response = requests.get(f"https://api.upbit.com/v1/ticker?markets={markets}", timeout=5)
    if response.status_code == 200:
        data = response.json()
        for item in data:
            ticker = item['market']
            trade_price = item.get('trade_price')
            if trade_price is not None:
                live_prices[ticker] = float(trade_price)
    else:
        print(f"Warning: Failed to fetch live prices, status {response.status_code}", file=sys.stderr)
except Exception as e:
    print(f"Warning: Error fetching live prices: {e}", file=sys.stderr)
    live_prices = {}

print("Live prices:", live_prices)

# 2. 각 티커 Turtle 신호 평가
alerts = []   # for LONG signals -> URGENT
close_calls = [] # for within 3% -> ALERT
errors = []

for ticker in TICKERS:
    try:
        s = TurtleStrategy(DB, ticker)
        df = s.load_daily_data()
        db_close = int(df["close"].iloc[-1])
        last_date = df.index[-1]

        # 실시간 가격 병합
        live_price = live_prices.get(ticker)
        close = db_close
        if live_price and live_price > 0:
            close = int(live_price)
            if str(last_date.date()) != today:
                fake_row = pd.DataFrame([{
                    "open": live_price, "high": live_price,
                    "low": live_price, "close": live_price,
                    "volume": 0, "volume_krw": 0,
                }], index=[pd.Timestamp(today)])
                df = pd.concat([df, fake_row])
            else:
                # 오늘 날짜 봉이 이미 존재하면 실시간 가격으로 OHLC 교체
                df.iloc[df.index.get_loc(last_date), df.columns.get_indexer(['open','high','low','close'])] = [live_price, live_price, live_price, live_price]

        # 실시간 가격 포함된 df로 진입 체크
        entry = s.check_entry(df)

        # DC High 직접 계산
        if len(df) >= 55:
            dc_high_val = int(df["high"].rolling(55).max().iloc[-1])
        else:
            dc_high_val = close
        pct_to_entry = round((dc_high_val - close) / close * 100, 1) if close > 0 else 999

        print(f"{ticker}: close={close}, dc_high={dc_high_val}, pct={pct_to_entry}, signal={entry['signal']}")

        if entry["signal"] == "LONG":
            alerts.append({
                "ticker": ticker, "price": close, "dc_high": dc_high_val,
                "pct": pct_to_entry, "reason": entry["reason"]
            })
        elif 0 < pct_to_entry < 3.0:
            close_calls.append({
                "ticker": ticker, "price": close, "dc_high": dc_high_val,
                "pct": pct_to_entry
            })
    except Exception as e:
        errors.append({"ticker": ticker, "error": str(e)[:60]})

print("\nAlerts:", alerts)
print("Close calls:", close_calls)
print("Errors:", errors)

# 3. 출력 - 요구사항에 맞게
if alerts:
    for a in alerts:
        print(f"긴급 {a['ticker']}: ₩{a['price']:,} (DC High: {a['dc_high']:,}, {a['pct']}% 남음)")
elif close_calls:
    for c in close_calls:
        print(f"경보 {c['ticker']}: ₩{c['price']:,} (DC High: {c['dc_high']:,}, {c['pct']}% 남음)")
else:
    print("NO_REPLY")
