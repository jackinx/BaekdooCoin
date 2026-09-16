# 백두코인봇 Upbit Crypto Trading Bot — Security Audit Report

**Audit Date:** 2026-08-22  
**Auditor:** Security Audit Team Lead (Automated Code Review)  
**Scope:** Full codebase — `engine.py`, `strategies/*.py`, `data_crawler.py`  
**Prior Review:** `qa_report.md` (22 bugs already identified and mostly fixed)

---

## Executive Summary

The 백두코인봇 codebase is a **single-machine trading bot** with a well-structured architecture and no external-facing services. The security posture is generally adequate for its deployment model — API keys are properly handled (not hardcoded), HTTPS is used, and no dangerous deserialization vectors exist. However, several **operational safety gaps** would become critical risks if the bot is connected to a live Upbit account with real funds.

### Risk Distribution

| Severity | Count | Description |
|----------|-------|-------------|
| CRITICAL | 1 | Missing emergency stop / kill switch for live trading |
| HIGH | 3 | SQL injection vector, no max trade limits, no order idempotency |
| MEDIUM | 5 | Path traversal risk, position size overflow, exception swallowing gaps |
| LOW | 6 | Memory/fd leaks, timestamp handling, WAL file exposure, input sanitization |
| **Total** | **15** | |

---

## 1. API Key Security

### Finding 1.1 — No API keys in codebase ✅

**Severity:** N/A (PASS)  
**File:** All `.py` files

No API keys, access tokens, or secrets of any kind are hardcoded in the source. The codebase does not yet include Upbit API authentication integration (the `data_crawler.py` uses the **public** candle API which requires no authentication). This is good: when auth integration is added, keys should be loaded from environment variables or a secrets manager.

**Verdict:** ✅ PASS — No hardcoded secrets.

### Finding 1.2 — Data directory permission check

**Severity:** LOW  
**File:** `data/` directory

The `data/upbit.db` file has permissions `rwx------` (owner-only), which is correct. However, there is **no `.gitignore` verification** in this audit — ensure `upbit.db` is never committed to version control.

**Recommendation:** Add `.gitignore` entry:
```
data/upbit.db
data/upbit.db-shm
data/upbit.db-wal
```

---

## 2. Input Validation

### Finding 2.1 — SQL Injection in `data_crawler.py` (f-string table names) 🟡

**Severity:** HIGH  
**File:** `data_crawler.py`, lines 105-112, 335-342  
**Description:** SQL table names are interpolated via Python f-strings (`f"CREATE INDEX ... ON {table}"`, `f"SELECT ... FROM {table}"`). While the `table` variable is currently sourced from the hardcoded `TIMEFRAME_CONFIG` dictionary keys (`["1m","5m","15m","1h","4h","1d","1w"]`), making exploitation nearly impossible today, this pattern is fragile:
- If the config is ever extended dynamically (e.g., from a config file or CLI argument), table names could be injectable.
- `INSERT_SQL.format(table=table)` at line 272 has the same issue.

**Exploitation Scenario:** If `TIMEFRAME_CONFIG` is populated from user input (e.g., a future feature allowing custom timeframes), an attacker could pass a table name like `"ohlcv_1d; DROP TABLE ohlcv_1d;--"` to destroy data.

**Fix:** Use parameterized table names via whitelist validation:
```python
ALLOWED_TABLES = {"ohlcv_1m", "ohlcv_5m", "ohlcv_15m", "ohlcv_1h", "ohlcv_4h", "ohlcv_1d", "ohlcv_1w"}
assert table in ALLOWED_TABLES, f"Invalid table name: {table}"
```

### Finding 2.2 — SQL Injection in strategy data loaders 🟢

**Severity:** N/A (PASS)  
**File:** All strategy files (`turtle.py`, `squeeze_momentum.py`, `bb_rsi_reversal.py`, `regime_detector.py`)

All strategy `load_data` methods use parameterized queries (`params=(ticker,)`). The table name is sourced from a hardcoded lookup map (`table_map`). This is safe.

**Verdict:** ✅ PASS — Parameterized queries, no injection surface.

### Finding 2.3 — Path Traversal in DB Path 🟡

**Severity:** MEDIUM  
**File:** `engine.py` (constructor), `data_crawler.py` (CLI args), all strategies

The `db_path` parameter is accepted as a raw string from callers without any path traversal validation. If `백두코인봇Engine(db_path="../../etc/passwd")` is instantiated, it would attempt to open that file. While SQLite would fail gracefully (it expects a valid DB), this could be used for information disclosure if the path is controllable from external input (e.g., a future web UI or API).

**Fix:** Validate the resolved path is within an expected directory:
```python
import os
ALLOWED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))
resolved = os.path.abspath(db_path)
if not resolved.startswith(ALLOWED_DIR):
    raise ValueError(f"DB path must be within {ALLOWED_DIR}")
```

### Finding 2.4 — Ticker string validation 🟢

**Severity:** N/A (PASS)  
**File:** Engine, all strategies

Ticker strings like `"KRW-BTC"` are used directly in SQL queries via parameterized placeholders. SQLite parameter binding prevents injection. No risk.

**Verdict:** ✅ PASS

### Finding 2.5 — No validation of zero/negative amounts 🟡

**Severity:** MEDIUM  
**File:** `engine.py` position sizing, `turtle.py:calculate_position_size`

**Description:**
- `turtle.py:calculate_position_size` checks `atr <= 0 or current_price <= 0` and returns 0, which is good.
- Squeeze/BB+RSI strategies in `engine.py` use `int(self.equity_krw * 0.01)` with no floor validation. The `max(pos_size, 5_000)` provides a floor but no protection against `equity_krw` being zero or negative.
- No check exists that `equity_krw` is positive at engine initialization.

**Exploitation Scenario:** If an upstream bug or configuration error sets `equity_krw=0`, orders of `₩5,000` would still be generated, which wastes API calls and could lead to rejected orders.

**Fix:** Add validation in `백두코인봇Engine.__init__`:
```python
if equity_krw <= 0:
    raise ValueError(f"equity_krw must be positive, got {equity_krw}")
```

### Finding 2.6 — Malformed Upbit API response handling 🟢

**Severity:** N/A (PASS)  
**File:** `data_crawler.py:_api_get`

The API client wraps calls in try/except with retry logic. Non-2xx responses raise `HTTPError` via `resp.raise_for_status()`, which is caught and retried. Edge case: if Upbit returns valid JSON that's missing expected keys (e.g., `candle["opening_price"]` doesn't exist), a `KeyError` will propagate as an unhandled exception in `fetch_candles`, which is caught by the caller's generic `except Exception`. This is acceptable for a data crawler — the worst case is a failed data fetch, not a security issue.

**Verdict:** ✅ PASS — Graceful degradation, no security impact.

---

## 3. Rate Limiting & API Safety

### Finding 3.1 — No emergency stop / kill switch 🔴

**Severity:** CRITICAL  
**File:** `engine.py`

**Description:** The engine has **no emergency shutdown mechanism**. If running in live trading mode and a bug causes it to generate erroneous orders (e.g., a flash crash triggers buy signals on all tickers simultaneously, or a logic error creates infinite buy-sell loops), there is no way to halt the bot programmatically besides killing the process. There is:
- No `EmergencyStopException` or similar signal handler
- No keyboard interrupt handling with graceful shutdown
- No max daily loss limit
- No max daily trade count limit
- No external kill-switch file (common pattern: watch for `/tmp/baekdoocoin_stop`)

**Exploitation Scenario:** If the bot is running unattended and enters a pathological state (market crash + bug causing repeated buy attempts), it could drain the account before a human notices.

**Fix (High Priority):**
1. Add a kill-switch file check at the start of each cycle: `if os.path.exists("/tmp/baekdoocoin_emergency_stop"): raise EmergencyStopError()`
2. Add `max_daily_trades` and `max_daily_loss_krw` parameters to the engine
3. Implement signal handler for SIGINT/SIGTERM to cancel pending orders

### Finding 3.2 — No max trade count or daily loss limit 🟡

**Severity:** HIGH  
**File:** `engine.py`

**Description:** The engine has no limits on:
- Maximum number of orders per cycle
- Maximum number of trades per day
- Maximum daily loss (as % of equity)

In live trading, a runaway condition could generate unlimited orders until the account is depleted.

**Fix:** Add configurable limits:
```python
self.max_orders_per_cycle = 10
self.max_daily_trades = 20
self.max_daily_loss_pct = 0.05  # 5% max daily loss
```

### Finding 3.3 — Position size upper bound missing 🟡

**Severity:** MEDIUM  
**File:** `turtle.py:calculate_position_size`, `engine.py`

**Description:** `calculate_position_size` can return values exceeding `equity_krw` if ATR is very small relative to price (e.g., a stablecoin with 0.01% daily volatility). The QA report noted this as BUG-5 (LOW) but it's worth elevating: if `atr/price = 0.001` (0.1%), then `unit_krw = equity_krw * 0.01 / 0.001 = equity_krw * 10`, producing an order 10x the account balance. The exchange would reject it, but the failure mode is ugly.

**Fix:** Cap at `equity_krw`:
```python
result = max(int(unit_krw), 5_000)
result = min(result, int(equity_krw))
```

### Finding 3.4 — API Rate Limiting in Crawler 🟢

**Severity:** N/A (PASS)  
**File:** `data_crawler.py`

The crawler implements:
- `RATE_LIMIT_SLEEP = 0.1` (10 req/s, within Upbit's ~10 req/s limit)
- Exponential backoff on 429 responses (`2**attempt` seconds)
- Retry logic with configurable `max_retries`

**Verdict:** ✅ PASS — Proper rate limiting and retry logic.

---

## 4. Data Integrity

### Finding 4.1 — SQLite WAL Mode ✅

**Severity:** N/A (PASS)  
**File:** `data_crawler.py:init_db`

SQLite is configured with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`, which provides good concurrent read access while the writer is active. The WAL file (`upbit.db-wal`) and shared memory file (`upbit.db-shm`) are properly in the `data/` directory alongside the DB file.

**Verdict:** ✅ PASS — Good SQLite configuration.

### Finding 4.2 — WAL/DB file exposure via version control 🟡

**Severity:** LOW  
**File:** `data/upbit.db`, `data/upbit.db-wal`, `data/upbit.db-shm`

The database files are 600 permission (owner-only), which is good. However, there's no `.gitignore` to prevent accidental commit of trade history.

**Recommendation:** Ensure `.gitignore` excludes all database files.

### Finding 4.3 — Timestamp handling — timezone-naive 🟡

**Severity:** LOW  
**File:** All files using `datetime`

All timestamp handling is timezone-naive:
- `datetime.now()` in `engine.py:run_cycle` uses local time, not UTC
- `pd.to_datetime()` parses timestamps from Upbit API which are ISO 8601 UTC strings (good)
- But the local ISO timestamp produced by `datetime.now().isoformat()` mixes local timezone into the result dict — if run across multiple timezones, cycle logs will be inconsistent

This is not a security issue directly but can cause operational confusion and timestamp-correlation bugs in cross-timezone deployments.

**Fix:** Use `datetime.now(timezone.utc).isoformat()` consistently.

### Finding 4.4 — NaN/Inf propagation guarded ✅

**Severity:** N/A (PASS)  
**File:** All strategy files

All entry/exit check methods properly guard against NaN using `pd.isna()` checks. Division by zero is guarded (e.g., `atr <= 0` in turtle size calculation, `/ volatility_ratio if volatility_ratio > 0`). The `float("inf")` used for profit factor when `gross_loss == 0` is intentional and safe.

**Verdict:** ✅ PASS — Good NaN/Inf hygiene.

---

## 5. Network Security

### Finding 5.1 — HTTPS enforced ✅

**Severity:** N/A (PASS)  
**File:** `data_crawler.py`

The API base URL is hardcoded as `https://api.upbit.com`. The `requests.Session` verifies TLS certificates by default (no `verify=False` anywhere).

**Verdict:** ✅ PASS — HTTPS with certificate verification.

### Finding 5.2 — No eval/exec/pickle 🟢

**Severity:** N/A (PASS)  
**File:** All files

No usage of `eval()`, `exec()`, `pickle.load()`, `marshal.load()`, `yaml.load()` (unsafe), `subprocess` with shell=True, or any other dangerous dynamic code execution or deserialization vectors were found.

**Verdict:** ✅ PASS — No code injection surface.

### Finding 5.3 — No authentication for crawler API calls 🟢

**Severity:** N/A (INFO)  
**File:** `data_crawler.py`

The Upbit candle API endpoints (`/v1/candles/*`) are public and require no authentication. This is acceptable because:
- Only market data is fetched, not account data
- No API keys are needed
- Rate limits still apply

**Verdict:** ✅ PASS — Appropriate for data collection use case.

---

## 6. Operational Safety

### Finding 6.1 — No order idempotency 🟡

**Severity:** HIGH  
**File:** `engine.py:run_cycle`

**Description:** The engine tracks open positions in `_open_positions` (added in BUG-19 fix), which prevents duplicate BUY orders while a position is open. However, this state is **in-memory only** — if the process restarts, all position tracking is lost. On restart:
- The engine has no way to know what's already held
- It could generate BUY orders for already-held positions
- There is no reconciliation with the actual exchange state

This is the most significant operational risk for live trading.

**Fix:**
1. Persist `_open_positions` state to the SQLite database or a JSON file on every change
2. On startup, query Upbit's `/v1/accounts` endpoint to discover current holdings
3. For each active position, create a persistent "order tracking" record with a unique `client_order_id` to prevent duplicate submission

### Finding 6.2 — No transaction safety for signal generation 🟡

**Severity:** MEDIUM  
**File:** `engine.py:run_cycle`

**Description:** If `run_cycle` crashes mid-cycle (after generating some orders but before completing), the partial order list is lost and not persisted anywhere. The `_open_positions` dict is also not atomically committed — it's possible for a position to be marked as opened but the corresponding order lost.

**Fix:** Save the order list to DB/persistent store *before* returning, with a status field:
```python
# At minimum, log all generated orders to a persistent store immediately
self._save_order_log(result["orders"])
```

### Finding 6.3 — Exception swallowing without recovery 🟡

**Severity:** MEDIUM  
**File:** `engine.py:run_cycle` (line 335)

The per-ticker exception handler catches all exceptions but only logs the error and assigns `"ERROR"` regime — it doesn't clean up any partial state changes that may have occurred for that ticker earlier in the cycle. If `_load_data` succeeds but the strategy call fails, the `_open_positions` could be left in an inconsistent state.

**Fix:** Add a finally block per-ticker or wrap the per-ticker logic in a transaction-like pattern.

### Finding 6.4 — Logging of sensitive data 🟢

**Severity:** N/A (PASS)  
**File:** All files

Logging is appropriately scoped:
- `logger.info` shows ticker names, signal types, and prices — all public market data
- No private keys, account balances, or PII are logged
- Log formatting uses Python's `%` style formatting with arguments separated from the format string, avoiding accidental injection

**Verdict:** ✅ PASS — No sensitive data leakage in logs.

### Finding 6.5 — No graceful shutdown for crawler 🟡

**Severity:** LOW  
**File:** `data_crawler.py`

The crawler's `collect()` loop has no interrupt handling. If killed mid-collection, partial data is committed (each `save_candles` commits immediately), so data consistency is preserved up to the last successful page fetch. This is acceptable behavior — no fix needed unless long-running crawls need to save progress and resume.

---

## 7. Structural Review

### Finding 7.1 — Circular imports guarded ✅

**Severity:** N/A (PASS)  
**File:** `strategies/__init__.py`, `engine.py`

The engine uses `try/except ImportError` to handle both `package.module` and direct script execution import paths. The strategy `__init__.py` properly exports all public symbols via `__all__`. No circular dependencies exist in the import graph.

**Verdict:** ✅ PASS.

### Finding 7.2 — No thread safety mechanisms 🟡

**Severity:** MEDIUM  
**File:** `engine.py`, `data_crawler.py`

**Description:** The codebase has zero thread safety considerations:
- `self._open_positions` is a plain dict with no locking
- SQLite connections are created, used, and closed in local scope (good)
- But the crawling `Session` object is shared across sequential calls

In the current single-threaded design this is fine. However, if the engine is ever wrapped in a scheduler that runs cycles concurrently (e.g., `concurrent.futures` or `asyncio`), the dict mutations become a data race.

**Recommendation:** Document the single-threaded assumption in class docstrings and add `threading.Lock` if concurrent access is planned.

### Finding 7.3 — DataFrame copies and memory usage 🟡

**Severity:** LOW  
**File:** All strategy files

Every strategy method starts with `df = df.copy()` when computing indicators, creating a full copy of the DataFrame. On a single call this is fine (a 1000-row OHLCV DataFrame is ~50KB), but if the engine runs every minute across 100+ tickers × multiple timeframes, the cumulative allocation overhead could become significant.

**Recommendation:** Use `df.copy()` only when mutating the DataFrame; for read-only indicator computation, add columns to a new DataFrame or use views.

### Finding 7.4 — DB connection lifecycle 🟢

**Severity:** N/A (PASS)  
**File:** All files

All SQLite connections are created locally, used, and explicitly closed — no connection pooling or connection leaks. The `UpbitCrawler` has a `close()` method for cleanup.

**Verdict:** ✅ PASS — Good resource management.

---

## 8. Compliance & Operational Readiness

### Finding 8.1 — No Upbit API Terms of Service compliance check 🟡

**Severity:** LOW  
**File:** N/A

The codebase does not include any reference to Upbit's API terms of service or usage policies. For a production deployment, consider:
- Upbit requires proper User-Agent headers (currently `"백두코인봇-Upbit-Crawler/1.0"` — good)
- Rate limits are respected (10 req/s — good)
- No account API endpoints are used without authentication (good)

### Finding 8.2 — No configuration validation on startup 🟡

**Severity:** LOW  
**File:** `engine.py:__init__`

The engine constructor accepts parameters with no validation beyond type hints:
- `tickers` list could be empty → should raise early
- `equity_krw` could be negative → should raise early

**Fix:** Add startup validation:
```python
if not tickers:
    raise ValueError("tickers list cannot be empty")
if equity_krw <= 0:
    raise ValueError("equity_krw must be positive")
```

---

## Summary of Findings by Category

| Category | CRITICAL | HIGH | MEDIUM | LOW | PASS |
|----------|----------|------|--------|-----|------|
| API Key Security | 0 | 0 | 0 | 1 | ✅ |
| Input Validation | 0 | 1 | 2 | 0 | 3 |
| Rate Limiting & API Safety | 1 | 1 | 1 | 0 | 1 |
| Data Integrity | 0 | 0 | 0 | 3 | 2 |
| Network Security | 0 | 0 | 0 | 0 | 4 |
| Operational Safety | 0 | 1 | 2 | 1 | 1 |
| Structure Review | 0 | 0 | 1 | 2 | 2 |
| **Total** | **1** | **3** | **6** | **7** | **13** |

---

## Top 5 Critical Fixes (Prioritized)

1. **🔴 [CRITICAL] Add emergency stop / kill switch** — Implement a file-watch kill switch (`/tmp/baekdoocoin_emergency_stop`), max daily loss limit, and max daily trade count before the bot is ever connected to a live Upbit account.

2. **🟡 [HIGH] Add order idempotency and position persistence** — Persist `_open_positions` to DB/file and reconcile with exchange state on startup to prevent duplicate orders after process restart.

3. **🟡 [HIGH] Add max trade limits** — Implement configurable `max_orders_per_cycle`, `max_daily_trades`, and `max_daily_loss_pct` to prevent runaway trading.

4. **🟡 [HIGH] Fix SQL injection risk in crawler** — Add whitelist validation for table names in `data_crawler.py` to prevent injection if config sources change in the future.

5. **🟡 [MEDIUM] Position size sanity cap** — Add `min(unit_krw, equity_krw)` cap in `calculate_position_size` and validate `equity_krw > 0` in engine constructor.

---

## Comparison with QA Report

The `qa_report.md` focused on **logic/strategy correctness bugs** (22 issues, primarily in signal generation and backtesting). This security audit covers **operational safety, data integrity, and hardening** — a complementary domain. Key differences:

| Aspect | QA Report | Security Audit |
|--------|-----------|----------------|
| Focus | Strategy logic bugs | Operational safety |
| Top issues | Donchian self-inclusion, noisy exits | Missing kill switch, no idempotency |
| Fixes applied | ✅ All CRITICAL/HIGH fixed | Manual implementation needed |

The BUG-19 fix (open position tracking) in the QA report partially addresses the duplicate-order concern, but without persistence it's still vulnerable to process restart.

---

## Conclusion

**Overall Risk Rating: MEDIUM-HIGH for live trading deployment**

The codebase is safe for backtesting and research use. For live trading:
- **Blocking issues:** Emergency stop missing, no order idempotency, no trade limits — **MUST FIX before going live**
- **Important hardening:** SQL injection hardening, position size caps, startup validation
- **Nice to have:** Timestamp UTC standardization, thread safety docs, memory optimization

The architecture is clean and well-structured. The core vulnerability is not in the individual components but in the operational safety net that should surround them before real money is involved.