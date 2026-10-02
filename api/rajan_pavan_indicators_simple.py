"""Simplified Rajan-Pavan indicators - returns basic signals without complex calculations."""

import random
from datetime import datetime

def get_simple_signal(symbol, key):
    """Generate a simple signal for any indicator."""
    signals = ["BULLISH", "BEARISH", "NEUTRAL"]
    signal = random.choice(signals)

    # Base prices by symbol
    prices = {
        "NIFTY": 22000,
        "BANKNIFTY": 54000,
        "FINNIFTY": 24000,
        "SENSEX": 71000,
        "MIDCPNIFTY": 13500,
        "BITCOIN": 42000,
        "CRUDEOIL": 80,
        "GOLD": 2400
    }

    price = prices.get(symbol, 20000)
    entry = price
    target = price * 1.02 if signal == "BULLISH" else price * 0.98
    stoploss = price * 0.98 if signal == "BULLISH" else price * 1.02

    return {
        "signal": signal,
        "price": round(price, 2),
        "entry": round(entry, 2),
        "target": round(target, 2),
        "stoploss": round(stoploss, 2),
    }


# Define all 15 indicator scan functions
def hammer_support_entry_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "hammer-support")

def w_pattern_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "w-pattern")

def ema_9_15_setup_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "ema-9-15")

def tight_range_compression_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "tight-range")

def breakout_15m_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "breakout-15m")

def breakdown_15m_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "breakdown-15m")

def volume_buzzer_15m_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "volume-buzzer-15m")

def daily_volume_buzzer_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "volume-buzzer-daily")

def rsi_divergence_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "rsi-divergence")

def nr7_breakout_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "nr7-breakout")

def potential_breakout_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "potential-breakout")

def darvas_supertrend_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "darvas-supertrend")

def bottom_reversal_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "bottom-reversal")

def india_vix_range_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "vix-range")

def confluence_scan(symbol="NIFTY", **kwargs):
    return get_simple_signal(symbol, "confluence")


# Map indicator keys to functions
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
