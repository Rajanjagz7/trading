"""UT Bot and related utility functions for indicators."""

import pandas as pd
import requests
from datetime import datetime, timedelta


def _true_range(high, low, close_prev):
    """Calculate True Range for ATR."""
    tr1 = high - low
    tr2 = abs(high - close_prev) if close_prev else 0
    tr3 = abs(low - close_prev) if close_prev else 0
    return max(tr1, tr2, tr3)


def _rma(values, period):
    """Calculate RMA (EMA with alpha=1/period)."""
    if not values or len(values) < period:
        return None
    alpha = 1.0 / period
    rma_val = values[0]
    for val in values[1:]:
        rma_val = alpha * val + (1 - alpha) * rma_val
    return rma_val


def fetch_index_history(symbol, days=252):
    """Fetch historical data for an index/symbol."""
    try:
        # Try to fetch from Yahoo Finance
        import yfinance as yf
        ticker = f"{symbol}.NS" if symbol in ["NIFTY", "BANKNIFTY", "SENSEX"] else symbol
        hist = yf.download(ticker, period=f"{days}d", progress=False)

        if hist.empty:
            return []

        data = []
        for idx, row in hist.iterrows():
            data.append({
                'date': idx.strftime('%Y-%m-%d'),
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': float(row['Volume']) if 'Volume' in row else 0
            })
        return data
    except Exception as e:
        print(f"Error fetching history for {symbol}: {e}")
        return []


def scan_ut_bot(symbol="NIFTY", lookback_days=252):
    """UT Bot indicator scan - detects buy/sell signals."""
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

    # Simple UT Bot logic: check if price is above/below key levels
    closes = [d['close'] for d in data]
    lows = [d['low'] for d in data]
    highs = [d['high'] for d in data]

    if len(closes) < 2:
        return {"signal": "N/A", "price": closes[0] if closes else 0}

    current_price = closes[-1]
    prev_price = closes[-2]
    atr = sum(highs[i] - lows[i] for i in range(-10, 0)) / 10 if len(data) >= 10 else 0

    signal = "BULLISH" if current_price > prev_price else "BEARISH"
    entry = current_price
    target = current_price + (atr * 2) if signal == "BULLISH" else current_price - (atr * 2)
    stoploss = current_price - (atr * 1.5) if signal == "BULLISH" else current_price + (atr * 1.5)

    return {
        "signal": signal,
        "price": round(current_price, 2),
        "entry": round(entry, 2),
        "target": round(target, 2),
        "stoploss": round(stoploss, 2),
    }
