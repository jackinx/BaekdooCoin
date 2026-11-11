# -*- coding: utf-8 -*-
"""지표 계산 (numpy only).

원본은 pandas로 구현했지만 이 PC(Windows 11 arm64 + Smart App Control)에서는
pandas C 확장이 차단되어 numpy로 재구현했다. 수식/파라미터는 원본과 동일.

모든 함수는 입력과 같은 길이의 np.ndarray(float64)를 반환하며,
계산 불가 구간은 np.nan 으로 채운다. (원본 pandas rolling 과 동일한 의미)
"""
from __future__ import annotations

import numpy as np


def _as_f(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def sma(x, n: int) -> np.ndarray:
    """단순이동평균 — pandas rolling(n).mean() 과 동일하게 NaN 을 견딘다.

    ⚠️ NaN 을 cumsum 으로 누적하면 이후 전 구간이 NaN 이 된다(원본 pandas 는 창 안에
    NaN 이 있을 때만 NaN). 창 안 유효값이 n 개일 때만 값을 낸다.
    """
    x = _as_f(x)
    out = np.full(x.shape, np.nan)
    if n <= 0 or len(x) < n:
        return out
    valid = np.isfinite(x)
    xv = np.where(valid, x, 0.0)
    cs = np.cumsum(np.insert(xv, 0, 0.0))
    cc = np.cumsum(np.insert(valid.astype(np.float64), 0, 0.0))
    s = cs[n:] - cs[:-n]
    cnt = cc[n:] - cc[:-n]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[n - 1:] = np.where(cnt >= n, s / np.maximum(cnt, 1.0), np.nan)
    return out


def ema(x, n: int) -> np.ndarray:
    """pandas ewm(span=n, adjust=False) 와 동일."""
    x = _as_f(x)
    out = np.full(x.shape, np.nan)
    if len(x) == 0 or n <= 0:
        return out
    alpha = 2.0 / (n + 1.0)
    acc = x[0]
    out[0] = acc
    for i in range(1, len(x)):
        acc = alpha * x[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def ewm_alpha(x, alpha: float) -> np.ndarray:
    """Wilder 계열 스무딩 (adjust=False)."""
    x = _as_f(x)
    out = np.full(x.shape, np.nan)
    if len(x) == 0:
        return out
    acc = x[0]
    out[0] = acc
    for i in range(1, len(x)):
        acc = alpha * x[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def rsi_wilder(close, n: int = 14) -> np.ndarray:
    """Wilder RSI (원본 regime_detector / bb_rsi 와 동일)."""
    c = _as_f(close)
    out = np.full(c.shape, np.nan)
    if len(c) < n + 1:
        return out
    delta = np.diff(c)
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    a = 1.0 / n
    avg_up = ewm_alpha(up, a)
    avg_dn = ewm_alpha(dn, a)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_up / avg_dn
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = np.where(avg_dn == 0, 100.0, rsi)
    out[1:] = rsi
    return out


def true_range(high, low, close) -> np.ndarray:
    h, l, c = _as_f(high), _as_f(low), _as_f(close)
    tr = np.full(h.shape, np.nan)
    if len(h) == 0:
        return tr
    tr[0] = h[0] - l[0]
    if len(h) > 1:
        prev_c = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)])
    return tr


def atr_wilder(high, low, close, n: int = 20) -> np.ndarray:
    """Wilder ATR. 첫 n개는 nan, 이후 ewm(alpha=1/n) 누적."""
    tr = true_range(high, low, close)
    out = np.full(tr.shape, np.nan)
    if len(tr) <= n:
        return out
    seed = np.nanmean(tr[1:n + 1])
    out[n] = seed
    prev = seed
    for i in range(n + 1, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def bollinger(close, n: int = 20, k: float = 2.0):
    c = _as_f(close)
    mid = sma(c, n)
    sd = np.full(c.shape, np.nan)
    if len(c) >= n:
        win = np.lib.stride_tricks.sliding_window_view(c, n)
        sd[n - 1:] = win.std(axis=1, ddof=1)   # pandas rolling().std()=표본표준편차
    return mid + k * sd, mid, mid - k * sd   # upper, mid, lower


def bb_width(upper, mid, lower) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (np.asarray(upper) - np.asarray(lower)) / np.asarray(mid)


def percentile_rank_last(series, window: int) -> float:
    """series 마지막 값이 최근 window 구간에서 차지하는 백분위(0~1)."""
    s = _as_f(series)
    vals = s[-(window + 1):]
    vals = vals[~np.isnan(vals)]
    if len(vals) < 10:
        return float("nan")
    cur = vals[-1]
    return float((vals < cur).sum() / (len(vals) - 1))


def adx(high, low, close, n: int = 14, smoothing: str = "sma"):
    """ADX. 반환: (adx, plus_di, minus_di)

    smoothing="sma"     : **기본값** — GitHub 업스트림 compute_adx 와 동일(단순이동평균).
                          원본 백테스트 수치와 호환된다.
    smoothing="wilder"  : 표준/TradingView 정의(RMA). 비교·실험용.
    """
    h, l, c = _as_f(high), _as_f(low), _as_f(close)
    size = len(h)
    nan = np.full(size, np.nan)
    if size < 2 * n + 1:
        return nan, nan.copy(), nan.copy()

    up_move = np.diff(h)                 # h[i]-h[i-1] (i=1..)
    dn_move = -np.diff(l)                # l[i-1]-l[i]
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)

    tr = true_range(h, l, c)[1:]
    a = 1.0 / n
    # 거래정지/도지 봉(atr=0)이나 첫 봉의 방향성 없음(0/0)이 EWM 시드로 전파되면
    # ADX가 전 구간 NaN 이 되어 그 종목은 영구히 TRENDING 이 될 수 없다 → 0으로 정규화.
    tr = np.where(np.isfinite(tr) & (tr > 0), tr, np.nan)
    plus_dm = np.where(np.isfinite(plus_dm), plus_dm, 0.0)
    minus_dm = np.where(np.isfinite(minus_dm), minus_dm, 0.0)

    if smoothing == "sma":
        atr_s = sma(np.nan_to_num(tr, nan=0.0), n)
        plus_s = sma(plus_dm, n)
        minus_s = sma(minus_dm, n)
    else:
        atr_s = ewm_alpha(np.nan_to_num(tr, nan=0.0), a)
        plus_s = ewm_alpha(plus_dm, a)
        minus_s = ewm_alpha(minus_dm, a)
    with np.errstate(divide="ignore", invalid="ignore"):
        safe_atr = np.where(atr_s > 0, atr_s, np.nan)
        pdi = 100.0 * plus_s / safe_atr
        mdi = 100.0 * minus_s / safe_atr
        denom = pdi + mdi
        dx = 100.0 * np.abs(pdi - mdi) / np.where(denom > 0, denom, np.nan)
    pdi = np.nan_to_num(pdi, nan=0.0, posinf=0.0, neginf=0.0)
    mdi = np.nan_to_num(mdi, nan=0.0, posinf=0.0, neginf=0.0)
    dx = np.nan_to_num(dx, nan=0.0, posinf=0.0, neginf=0.0)     # 첫 봉 0/0 → 0
    adx_vals = sma(dx, n) if smoothing == "sma" else ewm_alpha(dx, a)

    out_adx = np.full(size, np.nan)
    out_p = np.full(size, np.nan)
    out_m = np.full(size, np.nan)
    out_adx[1:] = adx_vals
    out_p[1:] = pdi
    out_m[1:] = mdi
    # Wilder 최초 n구간은 의미 없음 → nan 처리(원본과 동일한 워밍업)
    out_adx[:n] = np.nan
    out_p[:n] = np.nan
    out_m[:n] = np.nan
    return out_adx, out_p, out_m


def donchian(high, low, n: int):
    """Donchian 채널 — 원본과 동일하게 shift(1): '당일 제외 직전 n개'.

    dc_high[i] = max(high[i-n : i])  (i 시점 봉 제외)
    """
    h, l = _as_f(high), _as_f(low)
    size = len(h)
    dh = np.full(size, np.nan)
    dl = np.full(size, np.nan)
    if size <= n:
        return dh, dl
    hw = np.lib.stride_tricks.sliding_window_view(h, n)
    lw = np.lib.stride_tricks.sliding_window_view(l, n)
    # hw[k] = h[k : k+n] → i = k+n 시점의 '직전 n개'
    m = size - n
    dh[n:] = hw.max(axis=1)[:m]
    dl[n:] = lw.min(axis=1)[:m]
    return dh, dl


def linreg_slope(y, n: int) -> np.ndarray:
    """마지막 n개 창의 선형회귀 기울기 (LazyBear squeeze momentum 내부 계산과 동일)."""
    y = _as_f(y)
    out = np.full(y.shape, np.nan)
    if len(y) < n:
        return out
    x = np.arange(n, dtype=np.float64)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    win = np.lib.stride_tricks.sliding_window_view(y, n)
    out[n - 1:] = ((win - win.mean(axis=1, keepdims=True)) * (x - xm)).sum(axis=1) / denom
    return out


def value_when(y, i: int, n: int) -> float:
    """LazyBear value = linreg(y, n, offset=0) 의 마지막 값(기울기가 아니라 회귀선 값)."""
    y = _as_f(y)
    if len(y) < n + i:
        return float("nan")
    seg = y[len(y) - n - i: len(y) - i]
    x = np.arange(n, dtype=np.float64)
    xm = x.mean()
    ym = seg.mean()
    denom = ((x - xm) ** 2).sum()
    slope = ((x - xm) * (seg - ym)).sum() / denom
    intercept = ym - slope * xm
    return float(intercept + slope * (n - 1))


def keltner(high, low, close, n: int = 20, k: float = 1.5):
    mid = sma(close, n)
    rng = sma(_as_f(high) - _as_f(low), n)   # simple range ATR (squeeze 원본과 동일)
    return mid + k * rng, mid, mid - k * rng


__all__ = [
    "sma", "ema", "ewm_alpha", "rsi_wilder", "true_range", "atr_wilder",
    "bollinger", "bb_width", "percentile_rank_last", "adx", "donchian",
    "linreg_slope", "value_when", "keltner",
]
