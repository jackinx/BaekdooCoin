# Paperthin Code Review — BaekdooCoin 2026-08-27 Changes

**Reviewer:** DeepSeek V4 Flash (paperthin/fresh-eyes pass)
**Files:** `engine.py`, `live_monitor.py`, `regime_detector.py`, `risk.py`, `engine_mtf.py`
**Focus:** Readability, architecture, robustness, testability, naming, bugs.

---

## 🔴 Critical

### 1. [engine.py] `_emergency_stop_active` — no thread-safety for a life-critical flag

`_emergency_stop_active` is read in `_check_emergency_stop()` (called every cycle) and written by the signal handler callback `_handle_shutdown`. Neither access is synchronized.

```python
# Signal handler writes:
self._emergency_stop_active = True

# Cycle reads:
if self._emergency_stop_active:
    return "Emergency stop activated by signal handler"
```

Python GIL makes bool assignment atomic on CPython, but this is implementation-defined. The consequences of a missed write (running trades after SIGINT/SIGTERM) are severe. **Use `threading.Event()` or `atomic` flag instead.**

Same pattern applies to:
- `self._daily_trades_count` (updated inside `run_cycle()` loop, potentially racing with non-main-thread readers)
- `self._daily_last_date` (same)
- `self.equity_krw` (see #2)

### 2. [live_monitor.py] `engine.equity_krw` mutated externally without lock

In `_maybe_run_cycle()`:

```python
if self._upbit_client:
    try:
        live_krw = self._upbit_client.get_krw_balance()
        if live_krw > 0:
            self.engine.equity_krw = live_krw
    except Exception:
        pass
```

This races with `_check_emergency_stop()` reading `self.equity_krw` while the cycle evaluates positions. If the balance fetch lands mid-cycle, position sizing and loss-limit calculations use inconsistent equity values. This is a **shared-mutation-at-a-distance** pattern — the monitor reaches into engine internals. A proper `update_equity(krw)` method with internal locking would fix this.

### 3. [engine.py] `_persist_positions()` corrupts state on interrupted write

```python
def _persist_positions(self):
    pos_file = os.path.join(os.path.dirname(self.db_path), "open_positions.json")
    try:
        with open(pos_file, "w") as f:
            json.dump(self._open_positions, f, indent=2, default=str)
    except OSError as e:
        logger.warning("Failed to persist positions: %s", e)
```

If the engine crashes (SIGKILL, power loss) during the write, `open_positions.json` is left as a truncated/corrupt file. On restart, `_load_persisted_positions()` will silently skip recovery (`json.JSONDecodeError` caught), leaving the engine with **zero recognized positions** while the exchange still holds coins. This means the engine will attempt **new entries** on already-held coins (doubling up) and will **never generate exit orders** for those orphaned positions.

**Fix:** Write to a temp file, then `os.replace()` atomically.

### 4. [engine.py] NEUTRAL handler is a full code clone of TRENDING entry logic

The `else` branch (NEUTRAL regime) duplicates ~25 lines of the TRENDING block verbatim. The only differences are position sizing (1% of equity vs. Turtle's `calculate_position_size()`):

```python
# TRENDING entry:
pos_size = strategy.calculate_position_size(
    self.equity_krw, entry["atr"], entry["price"]
)
pos_size = min(pos_size, int(self.equity_krw))
...

# NEUTRAL entry:
pos_size = int(self.equity_krw * 0.01)
pos_size = min(max(pos_size, 5_000), int(self.equity_krw))
```

Duplication this large guarantees that any future change to the breakout detection logic (fake candle injection, DC calculation, order construction) must be made in **two places**. This will desync in practice. Extract to a shared `_try_turtle_entry(ticker, df_1d, live_prices, regime_context)` method.

### 5. [live_monitor.py] Fragile `TypeError` catch for `live_prices` kwarg

```python
try:
    raw_result = self.engine.run_cycle(live_prices=live_prices)
except TypeError:
    raw_result = self.engine.run_cycle()
```

This is a duck-typing antipattern: catching `TypeError` to detect whether the engine supports an optional parameter. A `TypeError` could mask a genuine bug inside `run_cycle()`. Either:
- Use `inspect.signature(self.engine.run_cycle)` to detect the parameter at init time, or
- Always use the MTF engine (which by inspection also lacks `live_prices` — see #6).

---

## 🟡 Medium

### 6. [engine_mtf.py] No `live_prices` parameter — MTF engine can't detect today's breakout

The legacy engine got `live_prices` injection for today's DC breakout detection. The MTF engine (`engine_mtf.py`) has **no equivalent parameter** in `run_cycle()`. Its breakout detection is purely candle-based. This means:

- On day 1 of a strong breakout, the MTF engine won't see today's (incomplete) candle and will miss the entry by ~1 day.
- The `live_monitor.py` already passes `live_prices` to the engine, but the MTF engine silently ignores it (falls through to the `except TypeError` catch in #5).

### 7. [regime_detector.py] `dc_high_override` and `dc_period` parameters undocumented

The new parameters are keyword-only (good pattern) but absent from the docstring:

```python
def detect_regime(self, df: pd.DataFrame, *,
                  dc_high_override: bool = True, dc_period: int = 55) -> Tuple[MarketRegime, dict]:
```

The docstring for `detect_regime` only documents the `df` parameter. Someone reading the docstring will not know about the DC override behavior. Add documentation that explains:
- What `dc_high_override` does (force TRENDING when 55-period high is breached)
- Why it exists (catch early breakouts before ADX catches up)
- What `dc_period` means

### 8. [regime_detector.py] `compute_adx()` recalculated every cycle — not cached

`compute_adx()` re-runs the full ADX calculation every call. While ADX is inherently window-based, it's called at least once per ticker per cycle. For 10 tickers × 60 cycles/hour, this is ~3600 ADX recalculations per hour of entirely stable data. Since the regime detector's signature doesn't accept pre-computed indicators, there's no way to cache between cycles. Consider a `_adx_cache: dict[str, (pd.Timestamp, pd.Series)]` with staleness check.

Same applies to `compute_bb_width`, `compute_rsi`, and `compute_ema` — they're all stateless and recompute from scratch.

### 9. [risk.py] `setdefault("carve_levels", [])` returns a shared mutable list

```python
carve_levels: list = position.setdefault("carve_levels", [])
```

`setdefault()` returns the existing value or sets the default and returns it. This creates a **shared mutable list** — if two `RiskManager` instances were ever used (unlikely currently, but not prevented), they'd share the same list object. The later `.sort()` and `.append()` calls mutate the list in place, which is a side effect on the caller's dict. This is intentional by design, but surprising for a newcomer.

A safer approach would be to not set defaults in the risk manager and require positions created via `make_position()` (which already initializes `carve_levels: []`).

### 10. [risk.py] `_EPS = 1e-8` is too small for meaningful float tolerance

```python
_EPS = 1e-8
```

Used in multiple threshold comparisons:
```python
if peak_gain_pct >= self.bone_activate_pct - _EPS:
if avg_gain_pct <= self.bone_final_lock_pct + _EPS:
if current_price <= carve_levels[-1] + _EPS:
```

`1e-8` is 0.000001% or 0.000001 KRW. This provides no real tolerance — it's pure floating-point noise handling. If a threshold is meant to be inclusive (≤), the epsilon should be meaningful (e.g., 0.01 for 1bp). If strict comparison is intended, use `<=` directly without epsilon; it's clearer.

### 11. [engine.py] Signal registration silently fails outside main thread

```python
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, _handle_shutdown)
    except ValueError:
        pass  # not in main thread — acceptable
```

"Acceptable" is debatable. The engine could be constructed in a background thread (e.g., by `live_monitor.py`'s init). If signal registration fails silently, the emergency stop via keyboard interrupt won't work. At minimum, emit a warning:

```python
except ValueError:
    logger.warning("Signal handler not registered (not in main thread)")
```

---

## 🟢 Minor

### 12. [engine.py] Commented-out logger with `%,.0f` — dead crash code

```python
# logger.info("📝 수동 포지션 등록: %s ₩%d @ ₩%,.0f (전략: %s)",
            # ticker, amount_krw, entry_price, strategy)
logger.info("📝 수동 포지션 등록: %s ₩%d @ ₩%d (전략: %s)",
            ticker, amount_krw, int(entry_price), strategy)
```

The commented-out line uses `%,.0f` format specifier which is **incompatible with Python's `%`-based logging** (same root cause as the `engine_mtf.py` fix). If someone uncomments it, the engine will crash at runtime. Remove the dead code entirely.

### 13. [regime_detector.py] RSI uses Python loop for Wilder's smoothing

```python
for i in range(period, len(avg_gain)):
    avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
```

This is ~100× slower than `df.ewm(alpha=1/period, adjust=False).mean()` for 500+ rows. For 1H data the dataset is small (typically < 500 rows), so the performance impact is negligible in practice. But for 5M data (500 rows loaded in MTF engine), this loop could be a perf hotspot. Replace with vectorized `ewm` for consistency.

### 14. [engine.py] `generate_orders()` is an unnecessary wrapper

```python
def generate_orders(self) -> list[dict]:
    cycle = self.run_cycle()
    return cycle["orders"]
```

What calls this? Nothing visible in the codebase. If external callers exist, fine — but if not, it's dead code. If it exists for API compatibility, document that.

### 15. [live_monitor.py] Awkward poll patterns

Two locations use the same pattern:
```python
# 2초 대기
for _ in range(20):
    if not self._running:
        break
    time.sleep(0.1)

# 다음 업데이트까지 5분 대기
for _ in range(300):
    if not self._running or self._emergency_stop:
        break
    time.sleep(1)
```

This is an idiom for "sleep with early exit on shutdown". It works but it's unusual. Consider `threading.Event.wait(timeout=60)` or `sleep(2) + running check` instead.

### 16. [risk.py] `was_10pct` uses `avg_gain_pct` not `peak_gain_pct`

```python
if avg_gain_pct >= self.was_10pct_threshold - _EPS:
    position["was_10pct_profit"] = True
```

This sets the `was_10pct` flag when the **current** price is ≥ +10%, not when the **peak** reached +10%. So if a coin hits 115%, drops back to 109%, and then rises again to 110%, `was_10pct` was **never set** (avg never hit 10%, only peak did). The comment says "EVER hit +10%", which implies peak-based, but the implementation is avg-based. This is a semantic mismatch. Either fix the comment or the code.

### 17. [engine_mtf.py] `_manage_position()` is a 60-line monolith

This method handles: exit, pyramid-up, averaging-down, and graduation — all in one deeply nested if/elif/return tree. This is impossible to unit test in isolation. Testing pyramid logic requires setting up a position where:
- `pnl_pct > 0` and `pos.units < max_pyramid + 1` and `current_price >= up_trigger`

And to test averaging-down, you need:
- `pnl_pct < 0` and `pos.units < max_units` and `current_price <= threshold`

There are 4 independent behaviors in one function. Each should be a separate method (`_try_pyramid`, `_try_avg_down`, `_check_exit`, `_check_graduation`).

### 18. [regime_detector.py] TRENDING condition ordering — ADX first, DC override second

```python
# 1. TRENDING: ADX > 25 + EMA 정배열
if current_adx > self.adx_threshold and ema_aligned:
    return MarketRegime.TRENDING, indicators
# 1b. DC High breakout override
if dc_high_override and len(df) >= dc_period:
    dc_high = df["high"].rolling(window=dc_period).max().iloc[-1]
    if current_close > dc_high:
        return MarketRegime.TRENDING, indicators
```

This ordering means: if ADX already says TRENDING, the DC override is **never checked**. This is correct (no need to check override when already trending) but it means the override's contribution to `indicators["details"]` is lost when ADX suffices. Minor — the override logs are merely not emitted. Not a bug.

### 19. [engine.py] Naming inconsistency — snake_case method on Korean-named class

`register_manual_position()` uses PEP8 snake_case. The class `백두코인봇Engine` and other methods (`_check_emergency_stop`, `_persist_positions`) mix Korean class names with Pythonic method names. This is cosmetic but inconsistent. Pick one convention.

### 20. [live_monitor.py] No exponential backoff on WebSocket reconnect

```python
except Exception as e:
    if self._running and not self._emergency_stop:
        logger.error("WebSocket 오류 — 5초 후 재접속: %s", e)
        time.sleep(5)
```

If the Upbit API is down for an extended period (maintenance, rate limiting), the monitor will busy-reconnect every 5s forever. Add exponential backoff (5s → 30s → 60s → 120s → cap at 300s) to avoid contributing to the outage.

---

## Summary

| Severity | Count | Key themes |
|----------|-------|------------|
| 🔴 Critical | 5 | Thread safety on emergency stop/equity; corrupt JSON persistence; duplicated NEUTRAL handler; fragile TypeError catch |
| 🟡 Medium | 6 | MTF missing `live_prices`; undocumented DC params; no ATR caching; shared mutable list in RiskManager; tiny epsilon; silent signal fail |
| 🟢 Minor | 8 | Dead comment code; RSI loop perf; `generate_orders` wrapper; awkward poll patterns; `was_10pct` semantic mismatch; monolith method; naming; no WS backoff |

**Most impactful finding:** The `open_positions.json` corruption bug (#3) is a latent critical issue. On a real restart scenario with funds in play, the engine will double-enter positions it already holds and never generate exits for them. This is time-bomb severity.

**Architecture note:** The legacy engine (`engine.py`) and MTF engine (`engine_mtf.py`) are evolving in parallel with partially overlapping features. The `live_monitor.py` code path tests MTF first and falls back to legacy, but each missing feature (like `live_prices` in MTF) creates silent differences in behavior. Consider either converging the interfaces or picking one primary engine.