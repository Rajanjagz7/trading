"""Trade Predictor indicator - EMA-based trend analysis."""


def _ema(values, period):
    """Calculate EMA (Exponential Moving Average)."""
    if not values or len(values) < period:
        return None

    alpha = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period  # SMA for first value

    for val in values[period:]:
        ema_val = alpha * val + (1 - alpha) * ema_val

    return ema_val


def scan_trade_predictor(symbol="NIFTY", lookback_days=252, ema_length=9, fast_length=9, slow_length=21):
    """Trade Predictor indicator - uses EMA crossovers for signals."""
    try:
        from ut_bot import fetch_index_history

        data = fetch_index_history(symbol, lookback_days)
        if not data:
            return {
                "signal": "N/A",
                "price": 0,
                "entry": 0,
                "target": 0,
                "stoploss": 0,
                "reason": "No data available"
            }

        closes = [d['close'] for d in data]
        if len(closes) < max(slow_length, ema_length) + 1:
            return {"signal": "NEUTRAL", "price": closes[-1] if closes else 0}

        # Calculate EMAs
        fast_ema = _ema(closes, fast_length)
        slow_ema = _ema(closes, slow_length)
        current_price = closes[-1]

        if fast_ema and slow_ema:
            if fast_ema > slow_ema:
                signal = "BUY"
                entry = current_price
                target = current_price * 1.02  # 2% target
                stoploss = current_price * 0.98  # 2% stop
            else:
                signal = "SELL"
                entry = current_price
                target = current_price * 0.98
                stoploss = current_price * 1.02
        else:
            signal = "NEUTRAL"
            entry = target = stoploss = current_price

        return {
            "signal": signal,
            "price": round(current_price, 2),
            "entry": round(entry, 2),
            "target": round(target, 2),
            "stoploss": round(stoploss, 2),
        }
    except Exception as e:
        return {
            "signal": "ERROR",
            "price": 0,
            "reason": str(e)
        }
