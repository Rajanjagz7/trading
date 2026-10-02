"""UT Bot and related utility functions for indicators."""


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


def fetch_index_history(symbol, days=252, lookback_days=None):
    """Fetch historical data for an index/symbol."""
    # Support both 'days' and 'lookback_days' parameters
    lookback = lookback_days if lookback_days is not None else days

    # Return mock data for testing - in production this should fetch real data
    data = []
    base_price = {"NIFTY": 20000, "BANKNIFTY": 40000, "SENSEX": 60000, "FINNIFTY": 20000, "MIDCPNIFTY": 10000}.get(symbol, 20000)

    for i in range(int(lookback)):
        data.append({
            'date': f'2026-{(9 - i//30 + 1):02d}-{(28 - i % 28):02d}',
            'open': base_price + (i * 10),
            'high': base_price + (i * 11),
            'low': base_price + (i * 9),
            'close': base_price + (i * 10.5),
            'volume': 1000000
        })

    return data
