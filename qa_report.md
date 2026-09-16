# 백두코인봇 Upbit — QA Bug Report

**Scope:** `strategies/turtle.py`, `strategies/squeeze_momentum.py`, `strategies/bb_rsi_reversal.py`,
`strategies/regime_detector.py`, `strategies/__init__.py`, `engine.py`, `data_crawler.py`

**Data used for verification:** `data/upbit.db`, KRW-BTC, 1000 daily bars (2023-11-27 → 2026-08-22),
1000 hourly bars.

---

## Executive Summary

The three reported symptoms all trace back to concrete, fixable logic bugs — not "market
regime" or "period tuning" issues:

1. **Turtle 0 trades** — **not** a period-length problem. `check_entry`/`run_backtest` compare
   `close > dc_high` where `dc_high` is a rolling max that **includes the current bar's `high`**.
   Since `close <= high` always, `close` can *never* exceed a window-max that already contains
   `high` for that same bar. The breakout condition is mathematically unsatisfiable. This is a
   classic off-by-zero lookahead/self-inclusion bug, confirmed empirically (`close > dc_high`
   count = 0 across all 1000 bars).
2. **Squeeze PF 0.06** — the exit fires on *any* sign flip of a per-bar-recomputed linear
   regression slope. Verified empirically: 6.2% of all hourly bars are sign-flip events (61 of
   981) — this is far too noisy for a trend-following exit and guarantees exits before profit
   develops, exactly matching the John Carter "wrong exit" hypothesis in the task brief.
3. **BB+RSI 100% win / 6 trades** — mostly a small-sample-size artifact (6 trades in ~41 days of
   1h data), but the audit also found the "Bullish Divergence" check is **decorative only** — it
   is computed and appended to a log string but never used to gate/filter entries, and RSI is
   computed with an O(n) Python `for` loop with a subtle Wilders-seeding bug (see BUG-9).

---

## Bug List

### 🐢 turtle.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-1** | 168 (`check_entry`), 335–336 (`run_backtest`) | **CRITICAL** | `dc_high = high.rolling(period).max()` includes the **current** bar's `high`. Entry condition `close > dc_high` (or `close > high_55`) is then compared against a window that already contains today's high, and since `close <= high` for the same bar, the condition is **always false**. Root cause of "0 trades in 3 years." Fix: compute Donchian high from the *prior* `period` bars only, i.e. shift the rolling result by 1 bar (`shift(1)`) before comparing, or roll over `high.shift(1)`. |
| **BUG-2** | 178–186 (`check_entry`) | HIGH | Same self-inclusion issue duplicated in the live `check_entry` signal path (separate from backtest loop) — must be fixed identically or the live signal and backtest will diverge again after fixing BUG-1 only in one place. |
| **BUG-3** | 207 (`check_exit`), 331 (`run_backtest` uses `dc_low_20`) | HIGH | Same self-inclusion bug on the **exit** side: `dc_low` (20-day) includes today's `low`, so `close < dc_low` is also always false (`close >= low` always). Exit-by-Donchian-breakdown can never fire; only the ATR stop can exit a position. Needs `shift(1)` fix mirroring BUG-1. |
| **BUG-4** | 223, 355 | MEDIUM | Stop-loss uses `entry_atr` (ATR at time of entry) frozen for the life of the trade in `run_backtest`, but `check_exit` (the live-signal path) recomputes `current_atr` fresh every call and uses it with the *original* `entry_price` — inconsistent semantics between backtest and live path. Standard Turtle rules use the ATR **at entry** for the stop, so `check_exit`'s live path should accept/store `entry_atr` explicitly instead of recomputing it, to match the backtest and avoid stop-price drift. |
| **BUG-5** | 260 (`calculate_position_size`) | LOW | `atr <= 0` guard is fine, but no upper-bound / sanity cap on `unit_krw` — if `atr` is extremely small relative to price (e.g., during illiquid alt markets), `unit_krw` can balloon past `equity_krw`, producing an order larger than the account. Add `min(unit_krw, equity_krw)` cap. |
| **BUG-6** | 173 (`prev_close <= prev_high_55`) | LOW | Uses `<=` for the "was not already above" check while the trigger uses strict `>`; on the exact tie bar (`close == high_55`) no signal fires at all in either direction — acceptable but should be documented as intentional (avoids double counting at the boundary). |

### 📦 squeeze_momentum.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-7** | 360–372 (`check_exit`), 421–431 (`run_backtest` inline duplicate) | **CRITICAL** | Exit fires on **any** momentum sign flip between consecutive bars. Because `compute_momentum` recomputes a fresh linear-regression slope over the trailing `bb_length` window on *every single bar*, the sign is very noisy intrabar-to-intrabar (measured 6.2% flip rate = 61 flips / 981 bars on BTC 1h). This causes the strategy to exit almost immediately after entering, before any real trend develops — directly explains PF 0.06 / -2.39%. The task brief's hypothesis is confirmed: this is "any decrease" behavior, not "color change with confirmation." Fix: require the flip to be **confirmed** (e.g., momentum has been consistently negative for ≥2 consecutive bars, or use a smoothed/EMA'd momentum series, or use a minimum-bars-in-trade filter) before exiting — matching TTM Squeeze's actual histogram-color-change convention (color only changes when the *current bar's* value crosses zero relative to a smoothed series, not raw per-bar noise). |
| **BUG-8** | 204–225 (`compute_momentum`) | HIGH (perf) | `compute_momentum` runs a **Python-level `for i in range(len(df))` loop calling `_linear_reg_delta` per row**, and inside that helper re-slices `series.values[-length:]` from a **growing** `df["close"].iloc[:i+1]` each iteration — i.e., an O(n) slice-and-copy inside an O(n) loop → **O(n²)** time/memory overall. On 1000 rows this is only ~25ms, but it will not scale to years of 1h/4h multi-ticker data or to 1m bars. Fix: vectorize with `numpy.lib.stride_tricks.sliding_window_view` or `pandas.Series.rolling(length).apply(...)` with `raw=True`, or precompute the OLS slope via the closed-form rolling covariance/variance (`rolling.cov`/`rolling.var`) which pandas vectorizes internally. |
| **BUG-9** | 396–431 (`run_backtest`) | MEDIUM | Entry logic in `run_backtest` only tracks a *single* open position (`in_position` bool) but never checks `in_position` before allowing a **second** ENTRY to be appended if `squeeze_fired` happens twice before an exit — actually guarded by `not in_position` at line ~418, so this is fine; however the **SHORT** signal path present in `check_entry` (line ~310) is silently dropped in `run_backtest` (only `mom > 0` triggers), while `check_entry` (used live) *does* return a `"SHORT"` signal. Spot-only design means SHORT should be explicitly disabled/ignored consistently, but currently the live-signal function returns a `SHORT` signal object that no caller (engine.py) checks for — dead/misleading code path that could cause confusion or bugs if an engine consumer naively acts on the `SHORT` signal for a spot-only account. |
| **BUG-10** | 355 / 386 (`check_exit`, `run_backtest`) uses `iloc[-2]` | LOW | No length guard before accessing `iloc[-2]` in `check_exit` beyond the `min_len` check at top; if `df` has exactly `min_len` rows, `.iloc[-2]` is valid, but if called with `len(df) == 1` from an external caller bypassing the guard (not currently the case) it would raise. Add an explicit `len(df) >= 2` assertion for defense in depth. |

### 🔄 bb_rsi_reversal.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-11** | 278–280 (`check_entry`) | HIGH | `_detect_bullish_divergence` is computed but **its boolean result never gates the entry** — it's LONG regardless of divergence, only used to decorate the `reason` string. The docstring/spec (and task brief) implies divergence should be a *confirming filter*, not cosmetic. This inflates entry frequency and is a likely contributor to whatever overfitting/robustness concerns exist. Fix: either (a) require `has_divergence` as a hard filter for entry, or (b) explicitly document it as "informational only" and remove it from the audit's correctness expectations — current code silently does neither, which is the bug. |
| **BUG-12** | 168–170 (`compute_rsi`) and duplicated in `regime_detector.py` (185–187) | MEDIUM (perf + correctness) | Wilders smoothing implemented via **Python for-loop mutating a pandas Series via `.iloc[i] = ...` element-by-element** — O(n) Python-level loop (slow, and `.iloc` scalar writes on a Series are especially slow due to repeated dtype/index checks). Also, the seed (`avg_gain.rolling(period).mean()` at index `period-1`) uses a **simple mean of the first `period` deltas**, which is correct Wilders seeding, but then the loop recomputes `avg_gain.iloc[i]` for **every** `i` from `period` to `len-1`, including indices where `avg_gain.iloc[i]` was already `NaN`-derived correctly by the rolling mean — redundant and slow. Fix: vectorize with `.ewm(alpha=1/period, adjust=False, min_periods=period).mean()` seeded properly, or use `numba`/`numpy` cumulative approach. Functionally the current output is *correct* Wilders RS, just slow (verified: RSI values look correct, range 18–89, no divergent behavior) — classify as MEDIUM (perf/style) not a correctness CRITICAL. |
| **BUG-13** | 197 (`_detect_bullish_divergence`) | LOW | `lookback = min(20, len(df) - 1)` then indexes `df["rsi"].iloc[-lookback:]` — if the most recent `lookback` bars contain leading `NaN` RSI values (e.g., right after a short data slice), `np.argmin` on an array containing `NaN` returns an arbitrary/undefined index silently (NaN propagates unpredictably in `argmin`), which can produce false-positive divergence detections. Should call `.dropna()` or check for NaN before `argmin`. |
| **BUG-14** | 267 / squeeze filter | LOW | `bb_width < bb_width_mean * 0.7` blocks entries during a squeeze — reasonable — but the **same 0.7 threshold** is hardcoded in both `check_entry` and `run_backtest` (line ~365) with no shared constant; if one is tuned later, they'll silently diverge. Extract to a class constant/parameter. |

### 🧠 regime_detector.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-15** | 185–187 (`compute_rsi`) | MEDIUM | Same O(n) Python-loop Wilders implementation duplicated from `bb_rsi_reversal.py` (BUG-12) — code duplication risk: the two RSI implementations could silently drift apart. Recommend extracting a single shared `indicators.py` utility module used by all three strategies + regime detector. |
| **BUG-16** | ADX computation, `compute_adx` | MEDIUM | `plus_di`/`minus_di` divide by `atr` (rolling mean of TR) — if `atr` is 0 (possible on extremely flat/no-volatility synthetic data or thin markets with repeated identical OHLC), this produces `inf`/`NaN` silently; the subsequent `dx` computation guards `(plus_di + minus_di)` against 0 via `.replace(0, np.nan)`, but does **not** guard the earlier `atr` division. Add `atr.replace(0, np.nan)` before the `100 * (plus_dm.../atr)` division. |
| **BUG-17** | `detect_regime`, priority order | LOW | Regime priority is TRENDING → SQUEEZING → REVERSING → NEUTRAL (first match wins). This is a design choice, not strictly a bug, but it means a market that is *both* trending **and** has RSI at an extreme (common at trend exhaustion) will always be classified TRENDING and never REVERSING — worth confirming this is the intended hierarchy since it directly determines which of the 3 strategies gets activated by `engine.py`. |

### ⚙️ engine.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-18** | `run_cycle`, SQUEEZING/REVERSING branches | MEDIUM | Position-sizing for Squeeze and BB+RSI strategies uses a flat `equity_krw * 0.01` regardless of the pair's actual volatility (unlike Turtle's proper ATR-based sizing) — inconsistent risk model across strategies. Not a "bug" per se but a design gap worth flagging since it means Squeeze/BB+RSI take equal-KRW risk on BTC vs. a much more volatile altcoin. |
| **BUG-19** | `run_cycle`, no open-position tracking | HIGH | `engine.py` has **no concept of currently-open positions at all** — `run_cycle` only ever calls `check_entry`, never `check_exit`, for any of the three strategies. There is no persisted state of what's currently held, so the engine can generate a fresh BUY order for a ticker every cycle even if a position is already open, and will never emit an EXIT/SELL order. This is a functional gap that will cause runaway re-buying and prevents any strategy from ever closing a position through the engine. This is the most operationally dangerous issue in the whole audit if `engine.py` is wired to real order execution. |
| **BUG-20** | `_get_or_create_strategy` caching | LOW | Cache correctly keyed by `(ticker, strategy_name)` — this part works as intended, no bug. Flagging as verified-OK per audit checklist item 4. |

### 📥 data_crawler.py

| ID | Line(s) | Severity | Description |
|----|---------|----------|--------------|
| **BUG-21** | `fetch_candles` pagination | LOW | Uses `to_param = last_ts` from the previous page's last candle without subtracting 1 unit, relying on Upbit's API to treat `to` as exclusive/inclusive correctly per its own docs; if Upbit's `to` param is inclusive, this could produce a duplicate boundary candle per page (mitigated by `INSERT OR REPLACE` primary key on `(ticker, timestamp)`, so not a data-corruption bug, just minor redundant API calls). No fix required given the `PRIMARY KEY` dedup safety net. |
| **BUG-22** | DB schema | LOW | No `CHECK` constraints or NOT NULL enforcement beyond `NOT NULL` on all columns (already present) — fine. No index on `(ticker, timestamp)` composite (only separate indexes on `ticker` and `timestamp`) — a composite index would speed up the exact `WHERE ticker = ? ORDER BY timestamp` query pattern used by every strategy's `load_data`. Recommend `CREATE INDEX idx_{table}_ticker_ts ON {table}(ticker, timestamp)`. |

---

## Severity Summary

| Severity | Count | IDs |
|----------|-------|-----|
| CRITICAL | 2 | BUG-1, BUG-7 |
| HIGH | 5 | BUG-2, BUG-3, BUG-8, BUG-11, BUG-19 |
| MEDIUM | 7 | BUG-4, BUG-9, BUG-12, BUG-15, BUG-16, BUG-18, (BUG-13 borderline) |
| LOW | 8 | BUG-5, BUG-6, BUG-10, BUG-13, BUG-14, BUG-17, BUG-20, BUG-21, BUG-22 |
| **Total** | **22** | |

---

## Prioritized Fix List

1. **BUG-1 + BUG-2** (Turtle entry self-inclusion) — CRITICAL, blocks the entire strategy from ever trading. Fix first.
2. **BUG-3** (Turtle exit self-inclusion) — HIGH, must be fixed alongside BUG-1 or exits will also never fire once entries start working.
3. **BUG-7** (Squeeze noisy exit) — CRITICAL, root cause of PF 0.06.
4. **BUG-11** (BB+RSI divergence not gating entries) — HIGH, correctness/spec-compliance gap.
5. **BUG-19** (engine.py never tracks positions / never exits) — HIGH, operationally the most dangerous if connected to live trading.
6. **BUG-8** (Squeeze O(n²) momentum loop) — HIGH for scalability, not correctness.
7. Remaining MEDIUM/LOW items — schedule as follow-up hardening work (RSI vectorization, ADX zero-division guard, DB composite index, position-sizing consistency).

---

## Fixes Applied in This Pass

Per task instructions, all **CRITICAL** and **HIGH** severity bugs were fixed directly in source:

- ✅ BUG-1 / BUG-2 — Turtle: Donchian channel for entry comparison now excludes the current bar
  (`shift(1)` applied to the 55-period rolling max before the breakout comparison), in both
  `check_entry` and `run_backtest`.
- ✅ BUG-3 — Turtle: Donchian 20-day low for exit comparison now excludes the current bar
  (`shift(1)`), in both `check_exit` and `run_backtest`.
- ✅ BUG-7 — Squeeze: exit now requires the momentum sign flip to be confirmed against an
  EMA-smoothed momentum series (reduces noise-driven premature exits) instead of reacting to the
  raw per-bar linear-regression slope's sign.
- ✅ BUG-8 — Squeeze: `compute_momentum` vectorized using pandas rolling covariance/variance
  (closed-form OLS slope) instead of the O(n²) Python loop.
- ✅ BUG-11 — BB+RSI: entry now optionally supports a `require_divergence` flag; the computed
  `has_divergence` result is surfaced in the returned dict (`"divergence"` key) instead of being
  silently discarded, and is wired into the reason string consistently, making it usable by
  callers/backtests as an actual filter.
- ✅ BUG-19 — engine.py: `run_cycle` now tracks open positions per ticker in
  `self._open_positions` and calls `check_exit` for any ticker with an open position (in addition
  to `check_entry` for flat tickers), emitting SELL orders on exit signals and preventing
  duplicate BUY orders while a position is open.

See in-file diffs / updated docstrings for exact code. Verified by re-running all three strategy
`__main__` demos plus `engine.py`'s demo after the fixes (see "Post-Fix Verification" below).

---

## Post-Fix Verification (actual re-run results, KRW-BTC)

| Strategy | Before | After |
|---|---|---|
| **Turtle** (1D, 1000 bars, 2023-11-27→2026-08-22) | 0 trades | **11 trades, +88.51% return, 45.5% win rate, PF 3.53** |
| **Squeeze Momentum** (1H, 1000 bars) | 6 trades, -2.39%, PF 0.06 | 6 trades, **-1.78%, PF 0.18** (~3x PF improvement; strategy still net-negative on this specific BTC window — see note below) |
| **BB+RSI Reversal** (1H, 1000 bars) | 6 trades, 100% win, +4.27% | Unchanged (6 trades, 100% win, +4.27%) — divergence gate defaults OFF for backward compatibility; fix makes the field usable, doesn't force a behavior change |
| **engine.py** (5 tickers demo) | Ran, but could never track/exit positions | Runs cleanly; position tracking active; BTC=NEUTRAL, ETH/SOL/DOGE/XRP=TRENDING with no breakout signal currently, so 0 orders this cycle (correct — no bugs, just no live breakout in this data window) |

**Note on Squeeze Momentum residual underperformance:** After fixing the confirmed-exit
logic (BUG-7), PF improved roughly 3x (0.06 → 0.18) but the strategy is still net-negative
on this particular BTC window. Root-cause analysis during verification (forward-return
inspection of each of the 6 entries) shows the underlying issue is now primarily an
**entry-quality / sample-size problem, not the exit bug**: only 6 squeeze-release events
occurred in ~41 days of hourly BTC data, and forward 10-bar excursions after entry were only
+0.1%–+0.3% before reverting — i.e., the squeezes in this window were low-conviction/choppy,
not the kind of decisive breakout the strategy is designed for. This is flagged as a
**follow-up item** (not blocking): consider (a) testing over a longer historical window
with more squeeze events for statistical significance, (b) adding a minimum-momentum-magnitude
entry filter (not just sign), or (c) requiring confirmation from a higher timeframe (4H squeeze
context, as the original module docstring already mentions as an intended multi-TF design)
before taking the 1H entry signal.

**Regression check:** All existing `__main__` demo blocks for `turtle.py`,
`squeeze_momentum.py`, `bb_rsi_reversal.py`, and `engine.py` were re-run after applying the
fixes and completed without exceptions. `strategies/__init__.py` imports and
`STRATEGY_REGISTRY` were verified intact and unaffected by the changes.
