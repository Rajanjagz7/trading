"""Rajan / Pavan Indicators — 15 Pine Script v6 indicators supplied by the app
owner in Rajan_TradingView_Verified_Indicators.md, translated to Python and
adapted to run on this app's existing daily index/commodity OHLCV series
(fetch_index_history in ut_bot.py) instead of a live TradingView chart.

Important adaptation notes (see the final delivery report for the full list):
  - The source Pine scripts were written for per-stock intraday/daily charts
    with real per-symbol volume. This app only has daily bars for a small
    fixed symbol universe (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY via the NSE
    index archive, SENSEX/BITCOIN/CRUDEOIL/GOLD via Yahoo) — there is no
    per-stock screener data source here, and the three "15-Minute" indicators
    (#5/#6/#7 in the source pack) are therefore computed against the same
    daily bar series as everything else rather than true 15-minute bars.
  - "Volume" for an index is a proxy (NSE's per-day index volume figure, or
    Yahoo's index-ticker volume) rather than genuine per-stock traded volume,
    so volume-based filters here are weaker signals than in the original
    per-stock screeners.
  - Weekly/monthly bars (needed for #7's optional weekly trend filter and
    #12's Darvas/Supertrend momentum filter) are derived locally from the
    daily series (grouped by ISO week / calendar month) rather than via
    Pine's request.security, since this app has no separate weekly/monthly
    feed. request.security(..., lookahead=barmerge.lookahead_off) semantics
    are reproduced by only ever reading the *previous, fully-closed* higher
    timeframe bar relative to each daily bar (see _htf_prev_value).
"""

from datetime import datetime

from trade_predictor import _ema
from ut_bot import _rma, _true_range, fetch_index_history

ALLOWED_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BITCOIN", "CRUDEOIL", "GOLD")


# ───────────────────────── generic numeric/series helpers ─────────────────────────

def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _clampi(value, lo, hi, default):
    return int(_clamp(value, lo, hi, default))


def _sma(values, length):
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i + 1 < length:
            continue
        out[i] = sum(values[i - length + 1:i + 1]) / length
    return out


def _rolling_max(values, length):
    """Inclusive of the current bar — matches Pine's ta.highest(x, length)."""
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i + 1 < length:
            continue
        out[i] = max(values[i - length + 1:i + 1])
    return out


def _rolling_min(values, length):
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i + 1 < length:
            continue
        out[i] = min(values[i - length + 1:i + 1])
    return out


def _shifted_rolling_max(values, length):
    """Matches Pine's ta.highest(x[1], length): the highest of the `length`
    bars ending on the PREVIOUS bar (current bar excluded)."""
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i < length:
            continue
        out[i] = max(values[i - length:i])
    return out


def _shifted_rolling_min(values, length):
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i < length:
            continue
        out[i] = min(values[i - length:i])
    return out


def _rolling_sum(values, length):
    n = len(values)
    out = [None] * n
    for i in range(n):
        if i + 1 < length:
            continue
        out[i] = sum(values[i - length + 1:i + 1])
    return out


def _rsi(closes, length):
    """Wilder's RSI — what Pine's ta.rsi() uses."""
    n = len(closes)
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = diff if diff > 0 else 0.0
        losses[i] = -diff if diff < 0 else 0.0
    avg_gain = _rma(gains, length)
    avg_loss = _rma(losses, length)
    out = [None] * n
    for i in range(n):
        if avg_gain[i] is None:
            continue
        if avg_loss[i] == 0:
            out[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            out[i] = 100 - (100 / (1 + rs))
    return out


def _macd(closes, fast=12, slow=26, signal=9):
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line


def _supertrend(bars, factor, atr_length):
    """Returns (line, is_uptrend) — is_uptrend True means the close is above
    the trailing supertrend line (Pine's direction < 0 / bullish state)."""
    atr = _rma(_true_range(bars), atr_length)
    n = len(bars)
    line = [None] * n
    up = [None] * n
    upper = [None] * n
    lower = [None] * n
    trend = True
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (bars[i]["high"] + bars[i]["low"]) / 2
        basic_upper = hl2 + factor * atr[i]
        basic_lower = hl2 - factor * atr[i]
        prev_i = i - 1
        if prev_i < 0 or upper[prev_i] is None:
            final_upper, final_lower = basic_upper, basic_lower
            trend = bars[i]["close"] >= final_lower
        else:
            prev_close = bars[prev_i]["close"]
            final_upper = basic_upper if (basic_upper < upper[prev_i] or prev_close > upper[prev_i]) else upper[prev_i]
            final_lower = basic_lower if (basic_lower > lower[prev_i] or prev_close < lower[prev_i]) else lower[prev_i]
            prev_trend = up[prev_i]
            close = bars[i]["close"]
            if prev_trend and close < final_lower:
                trend = False
            elif (not prev_trend) and close > final_upper:
                trend = True
            else:
                trend = prev_trend
        upper[i] = final_upper
        lower[i] = final_lower
        up[i] = trend
        line[i] = final_lower if trend else final_upper
    return line, up


def _pivot_high(values, left, right):
    n = len(values)
    out = [None] * n
    for i in range(left + right, n):
        p = i - right
        window = values[p - left:i + 1]
        if len(window) < left + right + 1:
            continue
        pv = values[p]
        if pv == max(window):
            out[i] = pv
    return out


def _pivot_low(values, left, right):
    n = len(values)
    out = [None] * n
    for i in range(left + right, n):
        p = i - right
        window = values[p - left:i + 1]
        if len(window) < left + right + 1:
            continue
        pv = values[p]
        if pv == min(window):
            out[i] = pv
    return out


def _resample_with_map(bars, period):
    """Groups daily bars into weekly ('W', ISO week) or monthly ('M', calendar
    month) OHLCV bars. Returns (grouped_bars, bucket_index_per_daily_bar)."""
    groups = []
    key_to_idx = {}
    bucket_idx = [0] * len(bars)
    for i, b in enumerate(bars):
        dt = datetime.strptime(b["date"], "%Y-%m-%d")
        if period == "W":
            iso = dt.isocalendar()
            key = (iso[0], iso[1])
        else:
            key = (dt.year, dt.month)
        if key not in key_to_idx:
            key_to_idx[key] = len(groups)
            groups.append({
                "date": b["date"], "open": b["open"], "high": b["high"], "low": b["low"],
                "close": b["close"], "volume": b.get("volume") or 0,
            })
        else:
            g = groups[key_to_idx[key]]
            g["high"] = max(g["high"], b["high"])
            g["low"] = min(g["low"], b["low"])
            g["close"] = b["close"]
            g["volume"] = (g.get("volume") or 0) + (b.get("volume") or 0)
        bucket_idx[i] = key_to_idx[key]
    return groups, bucket_idx


def _htf_prev_value(bucket_idx, htf_values):
    """For each daily bar, returns the higher-timeframe value from the last
    FULLY CLOSED higher-timeframe bar (never the still-forming current one) —
    this is what Pine's request.security(..., lookahead=barmerge.lookahead_off)
    yields on a daily chart."""
    out = [None] * len(bucket_idx)
    for i, b in enumerate(bucket_idx):
        out[i] = htf_values[b - 1] if b > 0 else None
    return out


def _validate_symbol(symbol):
    symbol = (symbol or "").upper()
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError(f"Rajan/Pavan indicators support {', '.join(ALLOWED_SYMBOLS)} only")
    return symbol


def _events_from_bars(rows, n=40):
    out = [
        {"date": r["date"], "signal": r["signal"], "note": r.get("note")}
        for r in rows if r.get("signal") and r["signal"] != "NONE"
    ]
    return out[-n:]


# ───────────────────────── #1 Uptrend Hammer Support Entry ─────────────────────────

def hammer_support_entry_scan(
    symbol, ema_length=50, use_structure=True, pivot_left=3, pivot_right=3,
    max_body_pct=0.35, min_lower_wick_body=2.0, max_upper_wick_body=0.5,
    support_distance_pct=1.0, lookback_days=180,
):
    symbol = _validate_symbol(symbol)
    ema_length = _clampi(ema_length, 2, 300, 50)
    pivot_left = _clampi(pivot_left, 1, 20, 3)
    pivot_right = _clampi(pivot_right, 1, 20, 3)
    lookback_days = _clampi(lookback_days, 30, 500, 180)

    fetch_days = lookback_days + ema_length + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= ema_length + pivot_left + pivot_right:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for a {ema_length}-period trend EMA")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(bars)

    ema_trend = _ema(closes, ema_length)
    ph = _pivot_high(highs, pivot_left, pivot_right)
    pl = _pivot_low(lows, pivot_left, pivot_right)

    prev_sh = last_sh = prev_sl = last_sl = None
    rows = []
    for i in range(n):
        if ph[i] is not None:
            prev_sh, last_sh = last_sh, ph[i]
        if pl[i] is not None:
            prev_sl, last_sl = last_sl, pl[i]

        higher_high = prev_sh is not None and last_sh is not None and last_sh > prev_sh
        higher_low = prev_sl is not None and last_sl is not None and last_sl > prev_sl
        ema_up = i > 0 and ema_trend[i] > ema_trend[i - 1]
        trend_filter = closes[i] > ema_trend[i] and ema_up
        structure_filter = higher_high and higher_low
        uptrend = trend_filter and (not use_structure or structure_filter)

        support = last_sl
        near_support = support is not None and abs(closes[i] - support) / closes[i] * 100 <= support_distance_pct

        o, h, l, c = bars[i]["open"], highs[i], lows[i], closes[i]
        body = abs(c - o)
        rng = h - l
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        hammer = (
            rng > 0 and body > 0
            and body / rng <= max_body_pct
            and lower_wick / body >= min_lower_wick_body
            and upper_wick / body <= max_upper_wick_body
        )

        hammer_entry = uptrend and near_support and hammer
        rows.append({
            "date": bars[i]["date"], "open": o, "high": h, "low": l, "close": c,
            "emaTrend": ema_trend[i], "support": support,
            "hammer": hammer, "uptrend": uptrend,
            "signal": "BUY" if hammer_entry else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "emaLength": ema_length, "useStructure": use_structure,
        "pivotLeft": pivot_left, "pivotRight": pivot_right,
        "columns": [
            {"key": "emaTrend", "label": "Trend EMA"},
            {"key": "support", "label": "Swing Support"},
            {"key": "hammer", "label": "Hammer", "fmt": "bool"},
        ],
        "current": {
            "date": last["date"], "close": last["close"], "emaTrend": last["emaTrend"],
            "support": last["support"], "signal": last["signal"],
        },
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #2 W Pattern Support Entry ─────────────────────────

def w_pattern_scan(symbol, pivot_left=3, pivot_right=3, ema_length=50, support_tolerance_pct=1.0, lookback_days=180):
    symbol = _validate_symbol(symbol)
    ema_length = _clampi(ema_length, 2, 300, 50)
    pivot_left = _clampi(pivot_left, 1, 20, 3)
    pivot_right = _clampi(pivot_right, 1, 20, 3)
    lookback_days = _clampi(lookback_days, 30, 500, 180)

    fetch_days = lookback_days + ema_length + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= ema_length + pivot_left + pivot_right:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for a {ema_length}-period trend EMA")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(bars)

    ema_trend = _ema(closes, ema_length)
    ph = _pivot_high(highs, pivot_left, pivot_right)
    pl = _pivot_low(lows, pivot_left, pivot_right)

    low1 = low2 = neckline = None
    rows = []
    for i in range(n):
        if pl[i] is not None:
            low1, low2 = low2, pl[i]
        if ph[i] is not None:
            neckline = ph[i]

        similar_lows = low1 is not None and low2 is not None and abs(low1 - low2) / low1 * 100 <= support_tolerance_pct
        uptrend = i > 0 and closes[i] > ema_trend[i] and ema_trend[i] > ema_trend[i - 1]
        near_support = low2 is not None and abs(closes[i] - low2) / closes[i] * 100 <= support_tolerance_pct
        w_setup = uptrend and similar_lows and near_support and neckline is not None
        w_breakout = w_setup and closes[i] > neckline

        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": highs[i], "low": lows[i], "close": closes[i],
            "emaTrend": ema_trend[i], "support": low2, "neckline": neckline,
            "signal": "BUY" if w_breakout else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "emaLength": ema_length, "pivotLeft": pivot_left, "pivotRight": pivot_right,
        "supportTolerancePct": support_tolerance_pct,
        "columns": [
            {"key": "emaTrend", "label": "Trend EMA"},
            {"key": "support", "label": "W Support"},
            {"key": "neckline", "label": "Neckline"},
        ],
        "current": {
            "date": last["date"], "close": last["close"], "support": last["support"],
            "neckline": last["neckline"], "signal": last["signal"],
        },
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #3 9/15 EMA Intraday Setup ─────────────────────────

def ema_9_15_setup_scan(
    symbol, fast_length=9, slow_length=15, use_trend=True, trend_length=50,
    use_volume=True, volume_length=20, volume_multiplier=1.0, signal_mode="crossover",
    lookback_days=150,
):
    symbol = _validate_symbol(symbol)
    fast_length = _clampi(fast_length, 2, 100, 9)
    slow_length = _clampi(slow_length, 2, 200, 15)
    trend_length = _clampi(trend_length, 2, 300, 50)
    volume_length = _clampi(volume_length, 2, 100, 20)
    lookback_days = _clampi(lookback_days, 30, 500, 150)
    signal_mode = "trend" if str(signal_mode).lower().startswith("trend") else "crossover"

    fetch_days = lookback_days + max(trend_length, slow_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= max(trend_length, slow_length) + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(bars)

    ema9 = _ema(closes, fast_length)
    ema15 = _ema(closes, slow_length)
    trend_ema = _ema(closes, trend_length) if use_trend else [None] * n
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)

    rows = []
    for i in range(n):
        volume_ok = (not use_volume) or (not has_volume) or (
            avg_volume[i] is not None and volumes[i] >= avg_volume[i] * volume_multiplier
        )
        bull_cross = i > 0 and ema9[i - 1] <= ema15[i - 1] and ema9[i] > ema15[i]
        bear_cross = i > 0 and ema9[i - 1] >= ema15[i - 1] and ema9[i] < ema15[i]
        bull_trend = closes[i] > ema9[i] and ema9[i] > ema15[i]
        bear_trend = closes[i] < ema9[i] and ema9[i] < ema15[i]

        trend_ok_bull = (not use_trend) or (trend_ema[i] is not None and closes[i] > trend_ema[i])
        trend_ok_bear = (not use_trend) or (trend_ema[i] is not None and closes[i] < trend_ema[i])

        if signal_mode == "trend":
            bull_signal = bull_trend and volume_ok and trend_ok_bull
            bear_signal = bear_trend and volume_ok and trend_ok_bear
        else:
            bull_signal = bull_cross and volume_ok and trend_ok_bull
            bear_signal = bear_cross and volume_ok and trend_ok_bear

        signal = "BUY" if bull_signal else "SELL" if bear_signal else "NONE"
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": bars[i]["low"],
            "close": closes[i], "ema9": ema9[i], "ema15": ema15[i], "trendEma": trend_ema[i],
            "signal": signal,
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "fastLength": fast_length, "slowLength": slow_length, "useTrend": use_trend,
        "trendLength": trend_length, "useVolume": use_volume, "signalMode": signal_mode,
        "volumeAvailable": has_volume,
        "columns": [
            {"key": "ema9", "label": "EMA 9"}, {"key": "ema15", "label": "EMA 15"},
            {"key": "trendEma", "label": "Trend EMA"},
        ],
        "current": {"date": last["date"], "close": last["close"], "ema9": last["ema9"], "ema15": last["ema15"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #4 Tight Range + EMA Compression ─────────────────────────

def tight_range_compression_scan(
    symbol, range_length=7, ema_fast_length=9, ema_slow_length=15,
    volume_length=20, volume_factor=0.8, ema_distance_pct=1.0, lookback_days=120,
):
    symbol = _validate_symbol(symbol)
    range_length = _clampi(range_length, 2, 60, 7)
    ema_fast_length = _clampi(ema_fast_length, 2, 100, 9)
    ema_slow_length = _clampi(ema_slow_length, 2, 200, 15)
    volume_length = _clampi(volume_length, 2, 100, 20)
    lookback_days = _clampi(lookback_days, 30, 500, 120)

    fetch_days = lookback_days + max(ema_slow_length, volume_length, range_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= ema_slow_length + range_length + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    rng = [h - l for h, l in zip(highs, lows)]
    n = len(bars)

    lowest_range = _rolling_min(rng, range_length)
    ema_fast = _ema(closes, ema_fast_length)
    ema_slow = _ema(closes, ema_slow_length)
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)
    breakout_high = _shifted_rolling_max(highs, range_length)

    compression_flags = [False] * n
    rows = []
    for i in range(n):
        nr_signal = lowest_range[i] is not None and rng[i] <= lowest_range[i]
        ema_distance = abs(ema_fast[i] - ema_slow[i]) / closes[i] * 100
        ema_compressed = ema_distance <= ema_distance_pct
        volume_contracted = (not has_volume) or (avg_volume[i] is not None and volumes[i] <= avg_volume[i] * volume_factor)
        compression = nr_signal and ema_compressed and volume_contracted
        compression_flags[i] = compression

        breakout = i > 0 and compression_flags[i - 1] and breakout_high[i] is not None and closes[i] > breakout_high[i]
        signal = "BREAKOUT" if breakout else ("COMPRESSING" if compression else "NONE")
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": highs[i], "low": lows[i], "close": closes[i],
            "emaFast": ema_fast[i], "emaSlow": ema_slow[i], "compression": compression,
            "signal": signal,
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "rangeLength": range_length, "emaFastLength": ema_fast_length,
        "emaSlowLength": ema_slow_length, "volumeLength": volume_length, "volumeFactor": volume_factor,
        "emaDistancePct": ema_distance_pct, "volumeAvailable": has_volume,
        "columns": [
            {"key": "emaFast", "label": "EMA Fast"}, {"key": "emaSlow", "label": "EMA Slow"},
            {"key": "compression", "label": "Compressed", "fmt": "bool"},
        ],
        "current": {"date": last["date"], "close": last["close"], "compression": last["compression"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #5 15-Min Breakout (daily-bar adaptation) ─────────────────────────

def breakout_15m_scan(symbol, lookback=20, volume_length=20, volume_multiplier=1.0, use_ema=False, ema_length=50, lookback_days=150):
    symbol = _validate_symbol(symbol)
    lookback = _clampi(lookback, 2, 200, 20)
    volume_length = _clampi(volume_length, 2, 100, 20)
    ema_length = _clampi(ema_length, 2, 300, 50)
    lookback_days = _clampi(lookback_days, 30, 500, 150)

    fetch_days = lookback_days + max(lookback, ema_length, volume_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= max(lookback, ema_length) + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(bars)

    previous_high = _shifted_rolling_max(closes, lookback)
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)
    ema = _ema(closes, ema_length) if use_ema else [None] * n

    rows = []
    for i in range(n):
        breakout = previous_high[i] is not None and closes[i] > previous_high[i]
        volume_confirmed = (not has_volume) or (avg_volume[i] is not None and volumes[i] > avg_volume[i] * volume_multiplier)
        trend_ok = (not use_ema) or (ema[i] is not None and closes[i] > ema[i])
        buy_signal = breakout and volume_confirmed and trend_ok
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": bars[i]["low"], "close": closes[i],
            "breakoutLevel": previous_high[i], "ema": ema[i],
            "signal": "BUY" if buy_signal else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "lookback": lookback, "volumeMultiplier": volume_multiplier, "useEma": use_ema,
        "volumeAvailable": has_volume,
        "columns": [{"key": "breakoutLevel", "label": f"{lookback}-Bar Level"}, {"key": "ema", "label": "Trend EMA"}],
        "current": {"date": last["date"], "close": last["close"], "breakoutLevel": last["breakoutLevel"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #6 15-Min Breakdown (daily-bar adaptation) ─────────────────────────

def breakdown_15m_scan(
    symbol, lookback=20, volume_length=20, volume_multiplier=1.0,
    use_rsi=False, rsi_length=14, rsi_max=40.0, use_macd=False, use_sma50=False, lookback_days=150,
):
    symbol = _validate_symbol(symbol)
    lookback = _clampi(lookback, 2, 200, 20)
    volume_length = _clampi(volume_length, 2, 100, 20)
    rsi_length = _clampi(rsi_length, 2, 100, 14)
    lookback_days = _clampi(lookback_days, 30, 500, 150)

    fetch_days = lookback_days + max(lookback, 50, volume_length, rsi_length, 26) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= max(lookback, 50) + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(bars)

    previous_low = _shifted_rolling_min(closes, lookback)
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)
    rsi = _rsi(closes, rsi_length)
    macd_line, signal_line = _macd(closes)
    sma50 = _sma(closes, 50)

    rows = []
    for i in range(n):
        breakdown_level = previous_low[i]
        volume_confirmed = (not has_volume) or (avg_volume[i] is not None and volumes[i] > avg_volume[i] * volume_multiplier)
        rsi_ok = (not use_rsi) or (rsi[i] is not None and rsi[i] <= rsi_max)
        macd_ok = (not use_macd) or (macd_line[i] < signal_line[i])
        sma_ok = (not use_sma50) or (sma50[i] is not None and closes[i] < sma50[i])
        breakdown = breakdown_level is not None and closes[i] < breakdown_level and volume_confirmed and rsi_ok and macd_ok and sma_ok
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": bars[i]["low"], "close": closes[i],
            "breakdownLevel": breakdown_level, "rsi": rsi[i],
            "signal": "SELL" if breakdown else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "lookback": lookback, "volumeMultiplier": volume_multiplier,
        "useRsi": use_rsi, "useMacd": use_macd, "useSma50": use_sma50, "volumeAvailable": has_volume,
        "columns": [{"key": "breakdownLevel", "label": f"{lookback}-Bar Level"}, {"key": "rsi", "label": "RSI"}],
        "current": {"date": last["date"], "close": last["close"], "breakdownLevel": last["breakdownLevel"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #7 15-Min Volume Buzzer (daily-bar adaptation) ─────────────────────────

def volume_buzzer_15m_scan(symbol, volume_multiplier=1.5, use_ema=True, ema_length=5, use_weekly_trend=False, weekly_ema_length=30, lookback_days=420):
    symbol = _validate_symbol(symbol)
    ema_length = _clampi(ema_length, 2, 100, 5)
    weekly_ema_length = _clampi(weekly_ema_length, 2, 100, 30)
    lookback_days = _clampi(lookback_days, 30, 730, 420)

    fetch_days = max(lookback_days, weekly_ema_length * 8 + 60)
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) < 10:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    has_volume = any(v for v in volumes)
    n = len(bars)

    ema = _ema(closes, ema_length)
    weekly_groups, bucket_idx = _resample_with_map(bars, "W")
    weekly_closes = [g["close"] for g in weekly_groups]
    weekly_ema = _ema(weekly_closes, weekly_ema_length)
    weekly_close_prev = _htf_prev_value(bucket_idx, weekly_closes)
    weekly_ema_prev = _htf_prev_value(bucket_idx, weekly_ema)

    rows = []
    for i in range(n):
        volume_spike = has_volume and i > 0 and volumes[i - 1] > 0 and volumes[i] > volumes[i - 1] * volume_multiplier
        price_trend_ok = (not use_ema) or closes[i] > ema[i]
        weekly_ok = (not use_weekly_trend) or (
            weekly_close_prev[i] is not None and weekly_ema_prev[i] is not None and weekly_close_prev[i] > weekly_ema_prev[i]
        )
        signal = volume_spike and price_trend_ok and weekly_ok
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": bars[i]["low"], "close": closes[i],
            "ema": ema[i], "volume": volumes[i],
            "signal": "BUZZ" if signal else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "volumeMultiplier": volume_multiplier, "useEma": use_ema,
        "useWeeklyTrend": use_weekly_trend, "weeklyEmaLength": weekly_ema_length, "volumeAvailable": has_volume,
        "columns": [{"key": "ema", "label": "EMA"}, {"key": "volume", "label": "Volume"}],
        "current": {"date": last["date"], "close": last["close"], "volume": last["volume"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #8 Daily Volume Buzzer ─────────────────────────

def daily_volume_buzzer_scan(symbol, volume_length=10, price_move_pct=2.0, lookback_days=90):
    symbol = _validate_symbol(symbol)
    volume_length = _clampi(volume_length, 2, 100, 10)
    lookback_days = _clampi(lookback_days, 30, 500, 90)

    fetch_days = lookback_days + volume_length + 30
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= volume_length + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    has_volume = any(v for v in volumes)
    avg_volume = _sma(volumes, volume_length)
    n = len(bars)

    rows = []
    for i in range(n):
        volume_condition = has_volume and avg_volume[i] is not None and volumes[i] > avg_volume[i]
        price_condition = i > 0 and closes[i] > closes[i - 1] * (1 + price_move_pct / 100)
        signal = volume_condition and price_condition
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": bars[i]["low"], "close": closes[i],
            "avgVolume": avg_volume[i], "volume": volumes[i],
            "signal": "BUZZ" if signal else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "volumeLength": volume_length, "priceMovePct": price_move_pct, "volumeAvailable": has_volume,
        "columns": [{"key": "volume", "label": "Volume"}, {"key": "avgVolume", "label": f"SMA({volume_length}) Vol"}],
        "current": {"date": last["date"], "close": last["close"], "volume": last["volume"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #9 RSI Divergence ─────────────────────────

def rsi_divergence_scan(symbol, rsi_length=14, pivot_left=5, pivot_right=5, lookback_days=150):
    symbol = _validate_symbol(symbol)
    rsi_length = _clampi(rsi_length, 2, 100, 14)
    pivot_left = _clampi(pivot_left, 1, 30, 5)
    pivot_right = _clampi(pivot_right, 1, 30, 5)
    lookback_days = _clampi(lookback_days, 30, 500, 150)

    fetch_days = lookback_days + rsi_length + pivot_left + pivot_right + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= rsi_length + pivot_left + pivot_right + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(bars)

    rsi = _rsi(closes, rsi_length)
    price_low = _pivot_low(lows, pivot_left, pivot_right)
    price_high = _pivot_high(highs, pivot_left, pivot_right)

    prev_price_low = prev_rsi_low = None
    prev_price_high = prev_rsi_high = None
    rows = []
    for i in range(n):
        bullish = False
        bearish = False
        if price_low[i] is not None:
            pivot_idx = i - pivot_right
            cur_rsi_low = rsi[pivot_idx] if pivot_idx >= 0 else None
            if prev_price_low is not None and cur_rsi_low is not None and prev_rsi_low is not None:
                bullish = price_low[i] < prev_price_low and cur_rsi_low > prev_rsi_low
            prev_price_low, prev_rsi_low = price_low[i], cur_rsi_low
        if price_high[i] is not None:
            pivot_idx = i - pivot_right
            cur_rsi_high = rsi[pivot_idx] if pivot_idx >= 0 else None
            if prev_price_high is not None and cur_rsi_high is not None and prev_rsi_high is not None:
                bearish = price_high[i] > prev_price_high and cur_rsi_high < prev_rsi_high
            prev_price_high, prev_rsi_high = price_high[i], cur_rsi_high

        signal = "BULL_DIV" if bullish else "BEAR_DIV" if bearish else "NONE"
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": highs[i], "low": lows[i], "close": closes[i],
            "rsi": rsi[i], "signal": signal,
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "rsiLength": rsi_length, "pivotLeft": pivot_left, "pivotRight": pivot_right,
        "columns": [{"key": "rsi", "label": "RSI"}],
        "current": {"date": last["date"], "close": last["close"], "rsi": last["rsi"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #10 NR7 Breakout Setup ─────────────────────────

def nr7_breakout_scan(symbol, nr_length=7, use_volume=True, volume_length=20, volume_multiplier=1.0, lookback_days=120):
    symbol = _validate_symbol(symbol)
    nr_length = _clampi(nr_length, 2, 60, 7)
    volume_length = _clampi(volume_length, 2, 100, 20)
    lookback_days = _clampi(lookback_days, 30, 500, 120)

    fetch_days = lookback_days + max(nr_length, volume_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= nr_length + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    rng = [h - l for h, l in zip(highs, lows)]
    n = len(bars)

    lowest_range = _rolling_min(rng, nr_length)
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)

    nr_high = nr_low = None
    rows = []
    for i in range(n):
        nr7 = lowest_range[i] is not None and rng[i] <= lowest_range[i]
        if nr7:
            nr_high, nr_low = highs[i], lows[i]

        volume_ok = (not use_volume) or (not has_volume) or (avg_volume[i] is not None and volumes[i] > avg_volume[i] * volume_multiplier)
        long_breakout = nr_high is not None and closes[i] > nr_high and volume_ok
        short_breakdown = nr_low is not None and closes[i] < nr_low and volume_ok

        signal = "BUY" if long_breakout else "SELL" if short_breakdown else ("NR7" if nr7 else "NONE")
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": highs[i], "low": lows[i], "close": closes[i],
            "nr7": nr7, "nrHigh": nr_high, "nrLow": nr_low, "signal": signal,
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "nrLength": nr_length, "useVolume": use_volume, "volumeAvailable": has_volume,
        "columns": [{"key": "nrHigh", "label": "NR7 High"}, {"key": "nrLow", "label": "NR7 Low"}, {"key": "nr7", "label": "NR7 Bar", "fmt": "bool"}],
        "current": {"date": last["date"], "close": last["close"], "nrHigh": last["nrHigh"], "nrLow": last["nrLow"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #11 Potential Breakout — 200D High + 30D Consolidation ─────────────────────────

def potential_breakout_scan(symbol, near_high_pct=5.0, consolidation_length=30, volume_length=50, minimum_close=0.0, lookback_days=180):
    symbol = _validate_symbol(symbol)
    consolidation_length = _clampi(consolidation_length, 2, 90, 30)
    volume_length = _clampi(volume_length, 2, 150, 50)
    lookback_days = _clampi(lookback_days, 30, 500, 180)

    fetch_days = lookback_days + consolidation_length + 200 + 30
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= consolidation_length + 200 + 5:
        raise RuntimeError(
            f"Not enough historical sessions returned ({len(bars)}) for a 200-day high + "
            f"{consolidation_length}-day consolidation scan"
        )

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    n = len(bars)

    high200 = _rolling_max(highs, 200)
    consolidation_high = _rolling_max(highs, consolidation_length)
    avg_volume = _sma(volumes, volume_length)
    has_volume = any(v for v in volumes)

    rows = []
    for i in range(n):
        near_200_high = high200[i] is not None and closes[i] >= high200[i] * (1 - near_high_pct / 100)
        old_high200_idx = i - consolidation_length
        old_high200 = high200[old_high200_idx] if old_high200_idx >= 0 else None
        consolidating = (
            consolidation_high[i] is not None and old_high200 is not None
            and consolidation_high[i] <= old_high200
        )
        volume_confirmed = (not has_volume) or (avg_volume[i] is not None and volumes[i] > avg_volume[i])
        price_filter = closes[i] > minimum_close
        candidate = near_200_high and consolidating and volume_confirmed and price_filter
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": highs[i], "low": bars[i]["low"], "close": closes[i],
            "high200": high200[i], "signal": "CANDIDATE" if candidate else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "nearHighPct": near_high_pct, "consolidationLength": consolidation_length,
        "volumeLength": volume_length, "minimumClose": minimum_close, "volumeAvailable": has_volume,
        "columns": [{"key": "high200", "label": "200D High"}],
        "current": {"date": last["date"], "close": last["close"], "high200": last["high200"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #12 Darvas + Supertrend Momentum ─────────────────────────

def darvas_supertrend_scan(symbol, st_atr_length=7, st_factor=3.0, rsi_length=14, rsi_minimum=40.0, use_volume=True, lookback_days=500):
    symbol = _validate_symbol(symbol)
    st_atr_length = _clampi(st_atr_length, 2, 50, 7)
    rsi_length = _clampi(rsi_length, 2, 100, 14)
    lookback_days = _clampi(lookback_days, 60, 730, 500)

    bars = fetch_index_history(symbol, lookback_days=lookback_days)
    if len(bars) <= max(st_atr_length, rsi_length) + 10:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    closes = [b["close"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    has_volume = any(v for v in volumes)
    n = len(bars)

    st, up = _supertrend(bars, st_factor, st_atr_length)
    rsi = _rsi(closes, rsi_length)

    weekly_groups, w_bucket = _resample_with_map(bars, "W")
    weekly_st, _ = _supertrend(weekly_groups, st_factor, st_atr_length)
    weekly_low_prev = _htf_prev_value(w_bucket, [g["low"] for g in weekly_groups])
    weekly_st_prev = _htf_prev_value(w_bucket, weekly_st)

    monthly_groups, m_bucket = _resample_with_map(bars, "M")
    monthly_st, _ = _supertrend(monthly_groups, st_factor, st_atr_length)
    monthly_low_prev = _htf_prev_value(m_bucket, [g["low"] for g in monthly_groups])
    monthly_st_prev = _htf_prev_value(m_bucket, monthly_st)

    rows = []
    for i in range(n):
        volume_ok = (not use_volume) or (not has_volume) or (i >= 2 and volumes[i] > volumes[i - 1] and volumes[i] > volumes[i - 2])
        daily_filter = st[i] is not None and lows[i] >= st[i] and rsi[i] is not None and rsi[i] > rsi_minimum and volume_ok
        weekly_ok = weekly_low_prev[i] is not None and weekly_st_prev[i] is not None and weekly_low_prev[i] >= weekly_st_prev[i]
        monthly_ok = monthly_low_prev[i] is not None and monthly_st_prev[i] is not None and monthly_low_prev[i] >= monthly_st_prev[i]
        signal = daily_filter and weekly_ok and monthly_ok
        rows.append({
            "date": bars[i]["date"], "open": bars[i]["open"], "high": bars[i]["high"], "low": lows[i], "close": closes[i],
            "supertrend": st[i], "rsi": rsi[i], "signal": "MOMENTUM" if signal else "NONE",
        })

    rows = rows[-min(len(rows), 260):]
    last = rows[-1]
    return {
        "symbol": symbol, "stAtrLength": st_atr_length, "stFactor": st_factor, "rsiLength": rsi_length,
        "rsiMinimum": rsi_minimum, "useVolume": use_volume, "volumeAvailable": has_volume,
        "columns": [{"key": "supertrend", "label": "Daily Supertrend"}, {"key": "rsi", "label": "RSI"}],
        "current": {"date": last["date"], "close": last["close"], "supertrend": last["supertrend"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #13 Bottom Reversal Confluence ─────────────────────────

def bottom_reversal_scan(
    symbol, volume_length=20, big_volume_multiplier=1.5, low_volume_multiplier=0.7,
    support_length=50, support_tolerance_pct=1.0, contraction_bars=4, lookback_days=150,
):
    symbol = _validate_symbol(symbol)
    volume_length = _clampi(volume_length, 2, 100, 20)
    support_length = _clampi(support_length, 5, 200, 50)
    contraction_bars = _clampi(contraction_bars, 3, 5, 4)
    lookback_days = _clampi(lookback_days, 30, 500, 150)

    fetch_days = lookback_days + max(volume_length, support_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= support_length + 2:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    has_volume = any(v for v in volumes)
    rng = [h - l for h, l in zip(highs, lows)]
    n = len(bars)

    avg_volume = _sma(volumes, volume_length)
    avg_range = _sma(rng, volume_length)
    support_series = _shifted_rolling_min(lows, support_length)
    tight_range = [False] * n
    shrinking_volume = [False] * n

    rows = []
    for i in range(n):
        red_candle = closes[i] < opens[i]
        big_red = red_candle and avg_range[i] is not None and rng[i] > avg_range[i] * 1.5
        low_volume_red = big_red and (not has_volume or (avg_volume[i] is not None and volumes[i] < avg_volume[i] * low_volume_multiplier))

        prev_selling_climax = (
            i > 0 and closes[i - 1] < opens[i - 1] and avg_range[i - 1] is not None
            and rng[i - 1] > avg_range[i - 1] * 1.5
            and (not has_volume or (avg_volume[i - 1] is not None and volumes[i - 1] >= avg_volume[i - 1] * big_volume_multiplier))
        )
        rejection = (
            prev_selling_climax and closes[i] > opens[i] and closes[i] > (highs[i - 1] + lows[i - 1]) / 2
            and (not has_volume or (avg_volume[i] is not None and volumes[i] >= avg_volume[i] * big_volume_multiplier))
        )

        support = support_series[i]
        near_support = support is not None and abs(closes[i] - support) / closes[i] * 100 <= support_tolerance_pct

        tight_range[i] = avg_range[i] is not None and rng[i] < avg_range[i]
        shrinking_volume[i] = (not has_volume) or (avg_volume[i] is not None and volumes[i] < avg_volume[i])

        contraction = False
        if i + 1 >= contraction_bars:
            tight_count = sum(1 for j in range(i - contraction_bars + 1, i + 1) if tight_range[j])
            shrink_count = sum(1 for j in range(i - contraction_bars + 1, i + 1) if shrinking_volume[j])
            contraction = tight_count >= contraction_bars - 1 and shrink_count >= contraction_bars - 1

        failed_breakdown = support is not None and lows[i] < support and closes[i] > support and closes[i] > opens[i]

        score = sum([low_volume_red, rejection, contraction, failed_breakdown, near_support])
        bottom_signal = score >= 3
        rows.append({
            "date": bars[i]["date"], "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i],
            "support": support, "score": score,
            "signal": "BOTTOM" if bottom_signal else "NONE",
            "note": f"{score}/5" if bottom_signal else None,
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "supportLength": support_length, "supportTolerancePct": support_tolerance_pct,
        "contractionBars": contraction_bars, "volumeAvailable": has_volume,
        "columns": [{"key": "support", "label": "Support"}, {"key": "score", "label": "Score /5"}],
        "current": {"date": last["date"], "close": last["close"], "support": last["support"], "score": last["score"], "signal": last["signal"]},
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── #14 India VIX Next-Day Range ─────────────────────────

def india_vix_range_scan(symbol, vix_divisor=1600.0, lookback_days=90):
    symbol = _validate_symbol(symbol)
    vix_divisor = _clamp(vix_divisor, 100, 10000, 1600.0)
    lookback_days = _clampi(lookback_days, 20, 400, 90)

    bars = fetch_index_history(symbol, lookback_days=lookback_days + 20)
    vix_bars = fetch_index_history("INDIAVIX", lookback_days=lookback_days + 20)
    if not bars or not vix_bars:
        raise RuntimeError("Not enough data returned for the underlying index or India VIX")

    vix_by_date = {b["date"]: b["close"] for b in vix_bars}
    rows = []
    for b in bars:
        vix_close = vix_by_date.get(b["date"])
        if vix_close is None:
            continue
        expected_move = b["close"] * vix_close / vix_divisor
        rows.append({
            "date": b["date"], "close": b["close"], "vix": vix_close,
            "expectedMove": expected_move,
            "upperRange": b["close"] + expected_move,
            "lowerRange": b["close"] - expected_move,
            "signal": "RANGE",
        })

    if not rows:
        raise RuntimeError("Could not align the underlying index and India VIX series on any common trading day")

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "vixDivisor": vix_divisor,
        "columns": [
            {"key": "vix", "label": "India VIX"}, {"key": "upperRange", "label": "Upper Range"},
            {"key": "lowerRange", "label": "Lower Range"}, {"key": "expectedMove", "label": "Expected Move"},
        ],
        "current": {
            "date": last["date"], "close": last["close"], "vix": last["vix"],
            "upperRange": last["upperRange"], "lowerRange": last["lowerRange"],
            "expectedMove": last["expectedMove"], "signal": "RANGE",
        },
        "bars": rows,
        "events": [],
    }


# ───────────────────────── #15 9/15 + Hammer + W Confluence ─────────────────────────

def confluence_scan(
    symbol, support_lookback=20, support_tolerance_pct=1.0, volume_length=20,
    use_volume=True, volume_multiplier=1.0, pivot_left=3, pivot_right=3, lookback_days=150,
):
    symbol = _validate_symbol(symbol)
    support_lookback = _clampi(support_lookback, 2, 120, 20)
    volume_length = _clampi(volume_length, 2, 100, 20)
    pivot_left = _clampi(pivot_left, 1, 20, 3)
    pivot_right = _clampi(pivot_right, 1, 20, 3)
    lookback_days = _clampi(lookback_days, 30, 500, 150)

    fetch_days = lookback_days + max(support_lookback, volume_length) + 40
    bars = fetch_index_history(symbol, lookback_days=fetch_days)
    if len(bars) <= support_lookback + pivot_left + pivot_right + 5:
        raise RuntimeError(f"Not enough historical sessions returned ({len(bars)}) for this scan")

    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    volumes = [b.get("volume") or 0 for b in bars]
    has_volume = any(v for v in volumes)
    n = len(bars)

    ema9 = _ema(closes, 9)
    ema15 = _ema(closes, 15)
    support_series = _shifted_rolling_min(lows, support_lookback)
    avg_volume = _sma(volumes, volume_length)
    ph = _pivot_high(highs, pivot_left, pivot_right)
    pl = _pivot_low(lows, pivot_left, pivot_right)

    first_low = second_low = neckline = None
    rows = []
    for i in range(n):
        if pl[i] is not None:
            first_low, second_low = second_low, pl[i]
        if ph[i] is not None:
            neckline = ph[i]

        support = support_series[i]
        near_support = support is not None and abs(closes[i] - support) / closes[i] * 100 <= support_tolerance_pct

        trend = (
            i > 0 and closes[i] > ema9[i] and ema9[i] > ema15[i]
            and ema9[i] > ema9[i - 1] and ema15[i] > ema15[i - 1]
        )

        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        body = abs(c - o)
        r = h - l
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        hammer = r > 0 and body > 0 and body / r <= 0.35 and lower_wick / body >= 2.0 and upper_wick / body <= 0.5

        similar_lows = first_low is not None and second_low is not None and abs(first_low - second_low) / first_low * 100 <= support_tolerance_pct
        w_breakout = similar_lows and neckline is not None and closes[i] > neckline

        volume_ok = (not use_volume) or (not has_volume) or (avg_volume[i] is not None and volumes[i] > avg_volume[i] * volume_multiplier)

        hammer_setup = trend and near_support and hammer
        w_setup = trend and w_breakout
        long_signal = trend and near_support and (hammer_setup or w_setup) and volume_ok

        rows.append({
            "date": bars[i]["date"], "open": o, "high": h, "low": l, "close": c,
            "ema9": ema9[i], "ema15": ema15[i], "support": support,
            "hammerSetup": hammer_setup, "wSetup": w_setup,
            "signal": "LONG" if long_signal else "NONE",
        })

    rows = rows[-lookback_days:]
    last = rows[-1]
    return {
        "symbol": symbol, "supportLookback": support_lookback, "supportTolerancePct": support_tolerance_pct,
        "useVolume": use_volume, "pivotLeft": pivot_left, "pivotRight": pivot_right, "volumeAvailable": has_volume,
        "columns": [
            {"key": "ema9", "label": "EMA 9"}, {"key": "ema15", "label": "EMA 15"}, {"key": "support", "label": "Support"},
        ],
        "current": {
            "date": last["date"], "close": last["close"], "ema9": last["ema9"], "ema15": last["ema15"],
            "support": last["support"], "signal": last["signal"],
        },
        "bars": rows,
        "events": _events_from_bars(rows),
    }


# ───────────────────────── dispatch table ─────────────────────────

RAJAN_PAVAN_KEYS = {
    "hammer-support": hammer_support_entry_scan,
    "w-pattern": w_pattern_scan,
    "ema-9-15": ema_9_15_setup_scan,
    "tight-range": tight_range_compression_scan,
    "breakout-15m": breakout_15m_scan,
    "breakdown-15m": breakdown_15m_scan,
    "volume-buzzer-15m": volume_buzzer_15m_scan,
    "volume-buzzer-daily": daily_volume_buzzer_scan,
    "rsi-divergence": rsi_divergence_scan,
    "nr7-breakout": nr7_breakout_scan,
    "potential-breakout": potential_breakout_scan,
    "darvas-supertrend": darvas_supertrend_scan,
    "bottom-reversal": bottom_reversal_scan,
    "vix-range": india_vix_range_scan,
    "confluence": confluence_scan,
}

# Maps the single endpoint's generic camelCase query params to each function's
# actual Python kwarg names — only keys present (non-None) in the incoming
# request are forwarded, so every function still falls back to its own
# Pine-matching default otherwise.
PARAM_MAP = {
    "hammer-support": {
        "lookbackDays": "lookback_days", "emaLength": "ema_length", "useStructure": "use_structure",
        "pivotLeft": "pivot_left", "pivotRight": "pivot_right", "maxBodyPct": "max_body_pct",
        "minLowerWickBody": "min_lower_wick_body", "maxUpperWickBody": "max_upper_wick_body",
        "supportDistancePct": "support_distance_pct",
    },
    "w-pattern": {
        "lookbackDays": "lookback_days", "pivotLeft": "pivot_left", "pivotRight": "pivot_right",
        "emaLength": "ema_length", "supportTolerancePct": "support_tolerance_pct",
    },
    "ema-9-15": {
        "lookbackDays": "lookback_days", "fastLength": "fast_length", "slowLength": "slow_length",
        "useTrend": "use_trend", "trendLength": "trend_length", "useVolume": "use_volume",
        "volumeLength": "volume_length", "volumeMultiplier": "volume_multiplier", "signalMode": "signal_mode",
    },
    "tight-range": {
        "lookbackDays": "lookback_days", "rangeLength": "range_length", "fastLength": "ema_fast_length",
        "slowLength": "ema_slow_length", "volumeLength": "volume_length", "volumeFactor": "volume_factor",
        "emaDistancePct": "ema_distance_pct",
    },
    "breakout-15m": {
        "lookbackDays": "lookback_days", "lookback": "lookback", "volumeLength": "volume_length",
        "volumeMultiplier": "volume_multiplier", "useEMA": "use_ema", "emaLength": "ema_length",
    },
    "breakdown-15m": {
        "lookbackDays": "lookback_days", "lookback": "lookback", "volumeLength": "volume_length",
        "volumeMultiplier": "volume_multiplier", "useRSI": "use_rsi", "rsiLength": "rsi_length",
        "rsiMax": "rsi_max", "useMACD": "use_macd", "useSMA50": "use_sma50",
    },
    "volume-buzzer-15m": {
        "lookbackDays": "lookback_days", "volumeMultiplier": "volume_multiplier", "useEMA": "use_ema",
        "emaLength": "ema_length", "useWeeklyTrend": "use_weekly_trend", "weeklyEmaLength": "weekly_ema_length",
    },
    "volume-buzzer-daily": {
        "lookbackDays": "lookback_days", "volumeLength": "volume_length", "priceMovePct": "price_move_pct",
    },
    "rsi-divergence": {
        "lookbackDays": "lookback_days", "rsiLength": "rsi_length", "pivotLeft": "pivot_left", "pivotRight": "pivot_right",
    },
    "nr7-breakout": {
        "lookbackDays": "lookback_days", "nrLength": "nr_length", "useVolume": "use_volume",
        "volumeLength": "volume_length", "volumeMultiplier": "volume_multiplier",
    },
    "potential-breakout": {
        "lookbackDays": "lookback_days", "nearHighPct": "near_high_pct", "consolidationLength": "consolidation_length",
        "volumeLength": "volume_length", "minimumClose": "minimum_close",
    },
    "darvas-supertrend": {
        "lookbackDays": "lookback_days", "stAtrLength": "st_atr_length", "stFactor": "st_factor",
        "rsiLength": "rsi_length", "rsiMinimum": "rsi_minimum", "useVolume": "use_volume",
    },
    "bottom-reversal": {
        "lookbackDays": "lookback_days", "volumeLength": "volume_length",
        "bigVolumeMultiplier": "big_volume_multiplier", "lowVolumeMultiplier": "low_volume_multiplier",
        "supportLength": "support_length", "supportTolerancePct": "support_tolerance_pct",
        "contractionBars": "contraction_bars",
    },
    "vix-range": {
        "lookbackDays": "lookback_days", "vixDivisor": "vix_divisor",
    },
    "confluence": {
        "lookbackDays": "lookback_days", "supportLookback": "support_lookback",
        "supportTolerancePct": "support_tolerance_pct", "volumeLength": "volume_length",
        "useVolume": "use_volume", "volumeMultiplier": "volume_multiplier",
        "pivotLeft": "pivot_left", "pivotRight": "pivot_right",
    },
}


def build_kwargs(key, raw):
    mapping = PARAM_MAP[key]
    out = {}
    for query_name, py_name in mapping.items():
        val = raw.get(query_name)
        if val is not None:
            out[py_name] = val
    return out
