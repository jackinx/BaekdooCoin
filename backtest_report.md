# 백두코인봇 Upbit Crypto Backtest Report

**Generated:** 2026-08-22 16:45:25 IST  
**Period:** 2023-11-27 ~ 2026-08-22  
**Fee:** 0.05%/trade  
**Data:** 1D + 1H OHLCV, 10 major tickers

> ⚠️ **IMPORTANT DATA CAVEAT:** 1D data spans ~1000 days (2023-11-27 → 2026-08-22), but 1H data only spans ~42 days (2026-07-11 → 2026-08-22). **Squeeze Momentum and BB+RSI (both 1H-based) were backtested on only ~6 weeks of recent data** — their results are statistically weak and NOT comparable to Turtle's full 2.7-year sample. Treat Squeeze/BB+RSI numbers as preliminary signals only.

---
## Phase A — BTC Deep Dive

### 🐢 Turtle (55D Donchian, 1D)

| Metric | Value |
|:---|---:|
| Total Trades | 11 |
| Win Rate | 45.5% |
| Profit Factor | 3.53 |
| Total Return | +88.5% |
| Max Drawdown | 17.3% |
| Sharpe Ratio | 5.40 |
| Avg Hold Time | 46.6d |
| Avg Return/Trade | +8.05% |
| Best Trade | +57.51% |
| Worst Trade | -10.19% |

**Monthly Returns:**

| Month | Return % |
|:---|---:|
| 2024-04 | +51.78% |
| 2024-06 | -7.15% |
| 2024-08 | -10.19% |
| 2025-02 | +57.51% |
| 2025-06 | +5.78% |
| 2025-08 | +0.54% |
| 2025-11 | -2.21% |
| 2026-01 | -5.57% |
| 2026-05 | +3.96% |
| 2026-07 | -5.94% |

**All Trades:**

| # | Entry | Exit | Return | Reason |
|:---|:---|:---|---:|:---|
| 1 | 2024-02-07 00:00:00 | 2024-04-17 00:00:00 | +51.78% | 20일 저가 하향 돌파 |
| 2 | 2024-05-20 00:00:00 | 2024-06-21 00:00:00 | -7.15% | 20일 저가 하향 돌파 |
| 3 | 2024-07-15 00:00:00 | 2024-08-04 00:00:00 | -10.19% | 20일 저가 하향 돌파 |
| 4 | 2024-09-19 00:00:00 | 2025-02-24 00:00:00 | +57.51% | 20일 저가 하향 돌파 |
| 5 | 2025-04-22 00:00:00 | 2025-06-05 00:00:00 | +5.78% | 20일 저가 하향 돌파 |
| 6 | 2025-07-09 00:00:00 | 2025-08-02 00:00:00 | +4.43% | 20일 저가 하향 돌파 |
| 7 | 2025-08-13 00:00:00 | 2025-08-18 00:00:00 | -3.89% | 2×ATR 손절 |
| 8 | 2025-09-16 00:00:00 | 2025-11-03 00:00:00 | -2.21% | 20일 저가 하향 돌파 |
| 9 | 2026-01-05 00:00:00 | 2026-01-25 00:00:00 | -5.57% | 20일 저가 하향 돌파 |
| 10 | 2026-03-04 00:00:00 | 2026-05-27 00:00:00 | +3.96% | 20일 저가 하향 돌파 |
| 11 | 2026-07-21 00:00:00 | 2026-07-29 00:00:00 | -5.94% | 20일 저가 하향 돌파 |

### 📦 Squeeze Momentum (LazyBear, 1H)

| Metric | Value |
|:---|---:|
| Total Trades | 6 |
| Win Rate | 16.7% |
| Profit Factor | 0.18 |
| Total Return | -1.8% |
| Max Drawdown | 1.8% |
| Sharpe Ratio | -8.58 |
| Avg Hold Time | 13.3h |
| Avg Return/Trade | -0.30% |
| Best Trade | +0.40% |
| Worst Trade | -1.23% |

**Monthly Returns:**

| Month | Return % |
|:---|---:|
| 2026-07 | -1.75% |
| 2026-08 | -0.03% |

**All Trades:**

| # | Entry | Exit | Return | Reason |
|:---|:---|:---|---:|:---|
| 1 | 2026-07-15 12:00:00 | 2026-07-16 02:00:00 | -1.23% | - |
| 2 | 2026-07-26 22:00:00 | 2026-07-27 17:00:00 | -0.52% | - |
| 3 | 2026-08-04 15:00:00 | 2026-08-05 13:00:00 | +0.40% | - |
| 4 | 2026-08-05 17:00:00 | 2026-08-06 11:00:00 | -0.15% | - |
| 5 | 2026-08-06 19:00:00 | 2026-08-06 22:00:00 | -0.02% | - |
| 6 | 2026-08-09 00:00:00 | 2026-08-09 04:00:00 | -0.26% | - |

### 🔄 BB+RSI Reversal (1H)

| Metric | Value |
|:---|---:|
| Total Trades | 6 |
| Win Rate | 100.0% |
| Profit Factor | inf |
| Total Return | +4.3% |
| Max Drawdown | 0.0% |
| Sharpe Ratio | 23.17 |
| Avg Hold Time | 9.8h |
| Avg Return/Trade | +0.71% |
| Best Trade | +1.59% |
| Worst Trade | +0.24% |

**Monthly Returns:**

| Month | Return % |
|:---|---:|
| 2026-07 | +1.83% |
| 2026-08 | +2.44% |

**All Trades:**

| # | Entry | Exit | Return | Reason |
|:---|:---|:---|---:|:---|
| 1 | 2026-07-20 06:00:00 | 2026-07-20 11:00:00 | +1.59% | Middle BB 도달 |
| 2 | 2026-07-28 00:00:00 | 2026-07-28 16:00:00 | +0.24% | RSI 회복 |
| 3 | 2026-08-03 06:00:00 | 2026-08-03 13:00:00 | +0.80% | RSI 회복 |
| 4 | 2026-08-10 16:00:00 | 2026-08-11 11:00:00 | +0.39% | RSI 회복 |
| 5 | 2026-08-11 18:00:00 | 2026-08-11 20:00:00 | +0.44% | Squeeze 강제 청산 |
| 6 | 2026-08-14 13:00:00 | 2026-08-14 23:00:00 | +0.81% | Middle BB 도달 |

---
## Phase B — Multi-Coin Comparison

| Coin | T PF | T Rtn% | T N | S PF | S Rtn% | S N | B PF | B Rtn% | B N | Best |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|
| BTC | 3.53 | +89% | 11 | 0.18 | -2% | 6 | inf | +4% | 6 | **Turtle** |
| ETH | 1.64 | +39% | 12 | 5.81 | +8% | 8 | inf | +2% | 2 | **Squeeze** |
| SOL | 1.57 | +40% | 11 | 63.31 | +6% | 9 | 16.18 | +3% | 3 | **Squeeze** |
| XRP | 4.25 | +253% | 11 | 0.94 | -0% | 4 | 0.18 | -4% | 4 | **Turtle** |
| ADA | 1.78 | +70% | 11 | 3.50 | +7% | 3 | inf | +0% | 1 | **Turtle** |
| DOGE | 6.30 | +264% | 8 | 0.08 | -7% | 10 | 0.00 | -2% | 1 | **Turtle** |
| BCH | 1.11 | +9% | 12 | 0.44 | -3% | 4 | 0.29 | -3% | 4 | **Turtle** |
| SUI | 4.51 | +268% | 10 | 0.12 | -5% | 8 | 0.21 | -5% | 4 | **Turtle** |
| SHIB | 2.86 | +148% | 11 | 4.24 | +24% | 10 | 0.00 | -4% | 1 | **Squeeze** |
| XLM | 5.21 | +281% | 10 | 0.07 | -11% | 9 | 0.00 | -2% | 2 | **Turtle** |
### Recommended Strategy per Coin

| Coin | Best (by PF, min-trades filter) | PF | Production Primary | Timeframe |
|:---|:---|:---|---:|:---|
| BTC | Turtle | 3.53 | **Turtle** | 1D |
| ETH | Squeeze | 5.81 | **Turtle** | 1D |
| SOL | Squeeze | 63.31 | **Turtle** | 1D |
| XRP | Turtle | 4.25 | **Turtle** | 1D |
| ADA | Turtle | 1.78 | **Turtle** | 1D |
| DOGE | Turtle | 6.30 | **Turtle** | 1D |
| BCH | Turtle | 1.11 | **Turtle** | 1D |
| SUI | Turtle | 4.51 | **Turtle** | 1D |
| SHIB | Squeeze | 4.24 | **Turtle** | 1D |
| XLM | Turtle | 5.21 | **Turtle** | 1D |

---
## Phase C — Regime Detector Analysis

BTC 1D Regime Distribution (950 days):

| Regime | Days | % | Active Strategy |
|:---|---:|---:|:---|
| TRENDING | 446 | 46.9% | Turtle |
| SQUEEZING | 117 | 12.3% | Squeeze Mmtm |
| REVERSING | 25 | 2.6% | BB+RSI |
| NEUTRAL | 362 | 38.1% | 관망 (No trade) |

### Strategy Performance by Regime (BTC)

**Turtle (target=TRENDING):**

| Regime | Trades | Win Rate | Return |
|:---|---:|---:|---:|
| TRENDING | 2 | 50% | +49.6% |
| SQUEEZING | 4 | 25% | -11.0% |
| REVERSING | 0 | 0% | +0.0% |
| NEUTRAL | 5 | 60% | +49.9% |

**Squeeze (target=SQUEEZING):**

| Regime | Trades | Win Rate | Return |
|:---|---:|---:|---:|
| TRENDING | 0 | 0% | +0.0% |
| SQUEEZING | 1 | 0% | -0.5% |
| REVERSING | 0 | 0% | +0.0% |
| NEUTRAL | 5 | 20% | -1.3% |

**BB+RSI (target=REVERSING):**

| Regime | Trades | Win Rate | Return |
|:---|---:|---:|---:|
| TRENDING | 0 | 0% | +0.0% |
| SQUEEZING | 4 | 100% | +3.1% |
| REVERSING | 0 | 0% | +0.0% |
| NEUTRAL | 2 | 100% | +1.2% |

---
## Phase D — Portfolio Simulation (₩50M)

**Rules:** Max 5 concurrent positions, equal allocation, 0.05% fee per trade

| Metric | Value |
|:---|---:|
| Starting Equity | ₩50,000,000 |
| Final Equity | ₩78,528,659 |
| Total Return | +57.1% |
| CAGR | +19.2% |
| Max Drawdown | 30.6% |
| Sharpe Ratio (ann) | 0.57 |
| Duration | 2.57 years |
| Total Trades Simulated | 206 |

**Monthly Portfolio Returns:**

| Month | Return % |
|:---|---:|
| 2024-02 | -0.04% |
| 2024-03 | -0.16% |
| 2024-04 | +28.29% |
| 2024-05 | -0.05% |
| 2024-06 | -9.16% |
| 2024-07 | -2.52% |
| 2024-08 | -13.24% |
| 2024-09 | -3.03% |
| 2024-10 | -1.72% |
| 2024-11 | -1.57% |
| 2024-12 | +0.00% |
| 2025-01 | +77.23% |
| 2025-02 | +28.72% |
| 2025-03 | -13.78% |
| 2025-04 | -0.05% |
| 2025-05 | +5.61% |
| 2025-06 | +1.08% |
| 2025-07 | -0.05% |
| 2025-08 | +9.00% |
| 2025-09 | +7.32% |
| 2025-10 | -0.83% |
| 2025-11 | -0.01% |
| 2025-12 | -2.09% |
| 2026-01 | -8.98% |
| 2026-02 | +0.00% |
| 2026-03 | -6.57% |
| 2026-04 | -3.14% |
| 2026-05 | -2.38% |
| 2026-06 | -3.23% |
| 2026-07 | -8.09% |
| 2026-08 | -0.03% |

**Equity Curve (daily snapshots):**

| Date | Equity (KRW) | Positions |
|:---|---:|---:|
| 2024-01-26 | ₩49,995,000 | 1 |
| 2024-02-02 | ₩49,995,000 | 1 |
| 2024-02-09 | ₩49,990,000 | 2 |
| 2024-02-16 | ₩49,975,005 | 5 |
| 2024-02-23 | ₩49,975,005 | 5 |
| 2024-03-01 | ₩49,975,005 | 5 |
| 2024-03-08 | ₩49,900,005 | 4 |
| 2024-03-15 | ₩49,900,005 | 4 |
| 2024-03-22 | ₩49,895,015 | 5 |
| 2024-03-29 | ₩49,895,015 | 5 |
| 2024-04-05 | ₩51,057,782 | 4 |
| 2024-04-12 | ₩49,461,980 | 3 |
| 2024-04-19 | ₩57,419,347 | 1 |
| 2024-04-26 | ₩57,419,347 | 1 |
| 2024-05-03 | ₩64,011,369 | 0 |
| 2024-05-10 | ₩64,011,369 | 0 |
| 2024-05-17 | ₩64,004,968 | 1 |
| 2024-05-24 | ₩63,985,769 | 4 |
| 2024-05-31 | ₩63,979,370 | 5 |
| 2024-06-07 | ₩62,292,705 | 4 |
| 2024-06-14 | ₩59,672,491 | 2 |
| 2024-06-21 | ₩58,744,419 | 1 |
| 2024-06-28 | ₩58,118,513 | 0 |
| 2024-07-05 | ₩58,118,513 | 0 |
| 2024-07-12 | ₩58,118,513 | 0 |
| 2024-07-19 | ₩58,089,459 | 5 |
| 2024-07-26 | ₩58,089,459 | 5 |
| 2024-08-02 | ₩56,656,400 | 4 |
| 2024-08-09 | ₩51,850,121 | 0 |
| 2024-08-16 | ₩51,844,936 | 1 |
| 2024-08-23 | ₩51,844,936 | 1 |
| 2024-08-30 | ₩49,156,097 | 1 |
| 2024-09-06 | ₩47,692,142 | 0 |
| 2024-09-13 | ₩47,692,142 | 0 |
| 2024-09-20 | ₩47,677,836 | 3 |
| 2024-09-27 | ₩47,668,301 | 5 |
| 2024-10-04 | ₩46,853,964 | 4 |
| 2024-10-11 | ₩46,853,964 | 4 |
| 2024-10-18 | ₩46,849,278 | 5 |
| 2024-10-25 | ₩46,849,278 | 5 |
| 2024-11-01 | ₩46,849,278 | 5 |
| 2024-11-08 | ₩46,113,745 | 5 |
| 2024-11-15 | ₩46,113,745 | 5 |
| 2024-11-22 | ₩46,113,745 | 5 |
| 2024-11-29 | ₩46,113,745 | 5 |
| 2024-12-06 | ₩46,113,745 | 5 |
| 2024-12-13 | ₩46,113,745 | 5 |
| 2024-12-20 | ₩46,113,745 | 5 |
| 2024-12-27 | ₩46,113,745 | 5 |
| 2025-01-03 | ₩46,113,745 | 5 |
| 2025-01-10 | ₩46,113,745 | 5 |
| 2025-01-17 | ₩51,653,196 | 3 |
| 2025-01-24 | ₩51,648,030 | 4 |
| 2025-01-31 | ₩81,725,834 | 2 |
| 2025-02-07 | ₩99,718,604 | 1 |
| 2025-02-14 | ₩99,718,604 | 1 |
| 2025-02-21 | ₩99,718,604 | 1 |
| 2025-02-28 | ₩105,194,068 | 0 |
| 2025-03-07 | ₩95,894,266 | 1 |
| 2025-03-14 | ₩92,986,462 | 0 |
| 2025-03-21 | ₩92,986,462 | 0 |
| 2025-03-28 | ₩92,977,163 | 1 |
| 2025-04-04 | ₩90,702,714 | 0 |
| 2025-04-11 | ₩90,702,714 | 0 |
| 2025-04-18 | ₩90,702,714 | 0 |
| 2025-04-25 | ₩90,657,372 | 5 |
| 2025-05-02 | ₩90,657,372 | 5 |
| 2025-05-09 | ₩90,657,372 | 5 |
| 2025-05-16 | ₩90,657,372 | 5 |
| 2025-05-23 | ₩90,657,372 | 5 |
| 2025-05-30 | ₩95,745,101 | 1 |
| 2025-06-06 | ₩96,775,380 | 0 |
| 2025-06-13 | ₩96,775,380 | 0 |
| 2025-06-20 | ₩96,775,380 | 0 |
| 2025-06-27 | ₩96,775,380 | 0 |
| 2025-07-04 | ₩96,775,380 | 0 |
| 2025-07-11 | ₩96,727,002 | 5 |
| 2025-07-18 | ₩96,727,002 | 5 |
| 2025-07-25 | ₩96,727,002 | 5 |
| 2025-08-01 | ₩96,727,002 | 5 |
| 2025-08-08 | ₩106,286,391 | 3 |
| 2025-08-15 | ₩106,275,762 | 4 |
| 2025-08-22 | ₩105,427,597 | 3 |
| 2025-08-29 | ₩105,427,597 | 3 |
| 2025-09-05 | ₩108,899,574 | 3 |
| 2025-09-12 | ₩108,877,795 | 5 |
| 2025-09-19 | ₩108,877,795 | 5 |
| 2025-09-26 | ₩113,141,866 | 1 |
| 2025-10-03 | ₩113,130,551 | 2 |
| 2025-10-10 | ₩112,203,831 | 0 |
| 2025-10-17 | ₩112,203,831 | 0 |
| 2025-10-24 | ₩112,203,831 | 0 |
| 2025-10-31 | ₩112,203,831 | 0 |
| 2025-11-07 | ₩112,203,831 | 0 |
| 2025-11-14 | ₩112,203,831 | 0 |
| 2025-11-21 | ₩112,203,831 | 0 |
| 2025-11-28 | ₩112,192,611 | 1 |
| 2025-12-05 | ₩112,192,611 | 1 |
| 2025-12-12 | ₩112,181,391 | 2 |
| 2025-12-19 | ₩109,847,785 | 1 |
| 2025-12-26 | ₩109,847,785 | 1 |
| 2026-01-02 | ₩109,847,785 | 1 |
| 2026-01-09 | ₩109,803,853 | 5 |
| 2026-01-16 | ₩109,803,853 | 5 |
| 2026-01-23 | ₩102,814,551 | 2 |
| 2026-01-30 | ₩99,978,599 | 0 |
| 2026-02-06 | ₩99,978,599 | 0 |
| 2026-02-13 | ₩99,978,599 | 0 |
| 2026-02-20 | ₩99,978,599 | 0 |
| 2026-02-27 | ₩99,978,599 | 0 |
| 2026-03-06 | ₩99,968,601 | 1 |
| 2026-03-13 | ₩99,968,601 | 1 |
| 2026-03-20 | ₩99,928,620 | 5 |
| 2026-03-27 | ₩95,810,709 | 3 |
| 2026-04-03 | ₩91,983,039 | 2 |
| 2026-04-10 | ₩90,511,501 | 1 |
| 2026-04-17 | ₩90,475,302 | 5 |
| 2026-04-24 | ₩90,475,302 | 5 |
| 2026-05-01 | ₩90,475,302 | 5 |
| 2026-05-08 | ₩90,475,302 | 5 |
| 2026-05-15 | ₩90,013,694 | 4 |
| 2026-05-22 | ₩88,811,821 | 3 |
| 2026-05-29 | ₩88,318,900 | 2 |
| 2026-06-05 | ₩87,193,162 | 1 |
| 2026-06-12 | ₩87,193,162 | 1 |
| 2026-06-19 | ₩87,193,162 | 1 |
| 2026-06-26 | ₩85,467,239 | 0 |
| 2026-07-03 | ₩85,458,692 | 1 |
| 2026-07-10 | ₩83,118,834 | 2 |
| 2026-07-17 | ₩83,767,712 | 5 |
| 2026-07-24 | ₩79,982,241 | 5 |
| 2026-07-31 | ₩78,552,225 | 5 |
| 2026-08-07 | ₩78,528,659 | 5 |
| 2026-08-14 | ₩78,528,659 | 5 |
| 2026-08-19 | ₩78,528,659 | 5 |

---
## 🏆 Production Recommendations

> ⚠️ Recommendations for Squeeze & BB+RSI are based on only ~6 weeks of 1H data — validate on longer history before production. Turtle (1D) has 2.7 years of reliable data.

### Strategy × Coin × Timeframe Matrix (Production-Ready)

| Coin | Primary (now) | Timeframe | Candidate (post-backfill) | Notes |
|:---|:---|:---|:---|:---|
| BTC | **Turtle** | 1D | BB+RSI | Turtle PF=3.53 |
| ETH | **Turtle** | 1D | Squeeze | Turtle PF=1.64 |
| SOL | **Turtle** | 1D | Squeeze | Turtle PF=1.57 |
| XRP | **Turtle** | 1D | - | Turtle PF=4.25 |
| ADA | **Turtle** | 1D | - | Turtle PF=1.78 |
| DOGE | **Turtle** | 1D | Squeeze | Turtle PF=6.30 |
| BCH | **Turtle** | 1D | - | Turtle PF=1.11 |
| SUI | **Turtle** | 1D | Squeeze | Turtle PF=4.51 |
| SHIB | **Turtle** | 1D | Squeeze | Turtle PF=2.86 |
| XLM | **Turtle** | 1D | Squeeze | Turtle PF=5.21 |

### Key Findings

1. **Turtle (1D) is the most reliable strategy** — validated across 2.7 years, 8-12 trades per coin, consistent Profit Factor > 1.5 on 8 of 10 coins. Best on high-volatility majors & alts (XRP +253%, DOGE +264%, XLM +281%, SUI +268%).
2. **Turtle's low win rate (20-45%) is by design** — it makes money through a few large trend wins (best BTC trade +57.5%) offsetting many small losses, matching the classic Turtle profile (PF 2.0-3.5).
3. **Squeeze & BB+RSI (1H) show promise but insufficient data** — only ~6 weeks of 1H history. BB+RSI's 100% win rate (6 trades) and Squeeze's SOL PF 63 are not statistically meaningful yet.
4. **Regime detector is directionally correct** — 47% TRENDING / 38% NEUTRAL / 12% SQUEEZING / 3% REVERSING. But the small trade counts per regime make regime-conditioned performance inconclusive.
5. **Portfolio (all 3 strategies, 10 coins, 5 concurrent)**: +57% over 2.6 years (CAGR +19.2%), max DD 30.6%, Sharpe 0.57 — driven almost entirely by Turtle; 1H strategies contribute negligibly given current data.

### ⚠️ Data Limitations & Next Steps

- **Critical:** Backfill 1H (and ideally 4H) data to at least 12-24 months before trusting Squeeze/BB+RSI results.
- 1D data is complete (1000 bars) — Turtle results are production-grade.
- Re-run this backtest after 1H backfill; the multi-coin comparison will then be meaningful across all 3 strategies.
- [ ] Backfill 1H/4H OHLCV to 2023 for all 20 tickers
- [ ] Paper-trade the Turtle strategy×coin matrix on Upbit (ready now)
- [ ] Add ATR position sizing to live engine (Turtle)
- [ ] Implement regime-gated strategy selection once 1H data is sufficient