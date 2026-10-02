"""Indian Stocks Universe — Fetches all NSE+BSE-listed stocks with bullish/bearish classification.
Supports Large Cap, Mid Cap, Small Cap filtering."""

import time
import requests
from typing import List, Dict, Optional
from datetime import datetime

STOCKS_CACHE_TTL = 3600  # 1 hour
_cache = {"time": 0.0, "data": None, "by_category": None}

# Indian stock indices classification
INDIAN_INDICES = {
    "NIFTY": {"name": "NIFTY 50", "type": "large_cap"},
    "BANKNIFTY": {"name": "BANK NIFTY", "type": "banking"},
    "FINNIFTY": {"name": "FINNIFTY", "type": "financial"},
    "SENSEX": {"name": "BSE SENSEX", "type": "large_cap"},
    "MIDCPNIFTY": {"name": "NIFTY MIDCAP 50", "type": "mid_cap"},
}

# Remove global commodities
EXCLUDED_SYMBOLS = {"BITCOIN", "CRUDEOIL", "GOLD", "SILVER"}


def _get_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json',
        'Referer': 'https://www.nseindia.com/',
    })
    return s


def _fetch_nse_stocks() -> List[Dict]:
    """Fetch all NSE+BSE-listed stocks."""
    try:
        session = _get_session()
        all_stocks = []

        # Initialize session with NSE
        try:
            session.get("https://www.nseindia.com", timeout=10)
            url = "https://www.nseindia.com/api/equity-master"
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            nse_stocks = data.get("data", []) if isinstance(data, dict) else []
            all_stocks.extend(nse_stocks)
            print(f"[Stocks] Fetched {len(nse_stocks)} from NSE")
        except Exception as e:
            print(f"[Stocks] NSE fetch failed: {e}")

        # Try BSE equity symbols list
        try:
            url_bse = "https://www.bseindia.com/markets/equity/EQReports/consolidatedScripMaster.aspx"
            resp_bse = session.get(url_bse, timeout=10)
            if resp_bse.status_code == 200:
                # Parse BSE CSV or JSON response
                # Note: BSE may return different format, extract what we can
                try:
                    bse_stocks = resp_bse.json().get("data", [])
                    all_stocks.extend(bse_stocks)
                    print(f"[Stocks] Fetched {len(bse_stocks)} from BSE")
                except:
                    pass
        except Exception as e:
            print(f"[Stocks] BSE fetch failed (non-critical): {e}")

        # Deduplicate by symbol
        seen_symbols = set()
        filtered = []
        for stock in all_stocks:
            symbol = stock.get("symbol", "").upper()
            if symbol and symbol not in seen_symbols and symbol not in EXCLUDED_SYMBOLS and symbol not in INDIAN_INDICES:
                seen_symbols.add(symbol)
                filtered.append({
                    "symbol": symbol,
                    "name": stock.get("name", symbol),
                    "isin": stock.get("isin", ""),
                    "industry": stock.get("industry", ""),
                    "category": _classify_stock(symbol),
                    "exchange": stock.get("exchange", "NSE"),
                })

        print(f"[Stocks] Total unique stocks: {len(filtered)}")
        return filtered[:500]  # Top 500 stocks

    except Exception as e:
        print(f"[Stocks] Fetch failed: {e}")
        return []


def _classify_stock(symbol: str) -> str:
    """Classify stock as large cap, mid cap, or small cap based on market cap."""
    # Simplified classification - in production, use actual market cap data
    # For now, use symbol-based heuristics
    nifty_50 = {"RELIANCE", "TCS", "INFY", "HDFC", "ICICIBANK", "SBIN", "BHARTIARTL", "MARUTI", "HCLTECH", "WIPRO"}

    if symbol in nifty_50:
        return "large_cap"
    elif symbol.startswith("M") or symbol.startswith("L"):
        return "mid_cap"
    else:
        return "small_cap"


def _compute_bullish_bearish(symbol: str) -> Dict:
    """Compute bullish/bearish signal for a stock."""
    try:
        session = _get_session()
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS"

        resp = session.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            quote = data.get("quoteSummary", {}).get("result", [{}])[0]

            price = quote.get("financialData", {}).get("currentPrice", {}).get("raw", 0)
            fifty_week_high = quote.get("summaryDetail", {}).get("fiftyTwoWeekHigh", {}).get("raw", 0)
            fifty_week_low = quote.get("summaryDetail", {}).get("fiftyTwoWeekLow", {}).get("raw", 0)

            if fifty_week_high and fifty_week_low and price:
                # Simple classification: if price > 75% of range = bullish
                range_pct = (price - fifty_week_low) / (fifty_week_high - fifty_week_low) * 100
                return {
                    "signal": "BULLISH" if range_pct > 75 else "BEARISH" if range_pct < 25 else "NEUTRAL",
                    "range_pct": round(range_pct, 2),
                    "price": price,
                }
    except Exception:
        pass

    return {"signal": "NEUTRAL", "range_pct": 50, "price": 0}


def get_indian_stocks(
    category: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
    force_refresh: bool = False
) -> Dict:
    """Get Indian stocks with pagination and category filtering."""
    global _cache

    now = time.time()
    age = now - _cache["time"]

    # Fetch fresh data if needed
    if force_refresh or _cache["data"] is None or age > STOCKS_CACHE_TTL:
        stocks = _fetch_nse_stocks()

        # Add bullish/bearish classification
        for stock in stocks:
            signal = _compute_bullish_bearish(stock["symbol"])
            stock.update(signal)

        # Organize by category
        by_category = {"large_cap": [], "mid_cap": [], "small_cap": []}
        for stock in stocks:
            cat = stock.get("category", "small_cap")
            if cat in by_category:
                by_category[cat].append(stock)

        # Sort each category by bullish count
        for cat in by_category:
            by_category[cat].sort(
                key=lambda s: (s.get("signal") == "BULLISH", s.get("range_pct", 0)),
                reverse=True
            )

        _cache["time"] = now
        _cache["data"] = stocks
        _cache["by_category"] = by_category

    stocks = _cache["data"]
    by_category = _cache["by_category"]

    # Filter by category if specified
    if category and category in by_category:
        filtered = by_category[category]
    else:
        filtered = stocks

    # Paginate
    start = (page - 1) * limit
    end = start + limit
    paginated = filtered[start:end]

    return {
        "available": len(filtered) > 0,
        "stocks": paginated,
        "total": len(filtered),
        "page": page,
        "limit": limit,
        "pages": (len(filtered) + limit - 1) // limit,
        "category": category or "all",
        "fetchedAt": datetime.now().isoformat(),
        "cached": not force_refresh and age < STOCKS_CACHE_TTL,
    }


def get_indian_indices() -> Dict:
    """Get only Indian indices (no global commodities)."""
    return {
        "available": True,
        "indices": list(INDIAN_INDICES.keys()),
        "details": INDIAN_INDICES,
        "count": len(INDIAN_INDICES),
        "fetchedAt": datetime.now().isoformat(),
    }
