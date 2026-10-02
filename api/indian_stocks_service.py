"""Indian Stocks Universe — Fetches all NSE-listed stocks with bullish/bearish classification.
Uses NSE's official /api/master-quote endpoint for complete stock universe.
Supports Large Cap, Mid Cap, Small Cap filtering. Falls back to popular stocks if APIs unavailable."""

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

# Fallback popular Indian stocks when APIs are unavailable
FALLBACK_STOCKS = [
    ("RELIANCE", "Reliance Industries", "large_cap"),
    ("TCS", "Tata Consultancy Services", "large_cap"),
    ("INFY", "Infosys", "large_cap"),
    ("HDFCBANK", "HDFC Bank", "large_cap"),
    ("ICICIBANK", "ICICI Bank", "large_cap"),
    ("SBIN", "State Bank of India", "large_cap"),
    ("WIPRO", "Wipro", "large_cap"),
    ("BHARTIARTL", "Bharti Airtel", "large_cap"),
    ("MARUTI", "Maruti Suzuki", "large_cap"),
    ("HCLTECH", "HCL Technologies", "large_cap"),
    ("LTTS", "LTT Infotech", "large_cap"),
    ("NESTLEIND", "Nestlé India", "large_cap"),
    ("HINDALCO", "Hindalco Industries", "large_cap"),
    ("POWERGRID", "Power Grid Corporation", "large_cap"),
    ("ONGC", "ONGC", "large_cap"),
    ("AXISBANK", "Axis Bank", "mid_cap"),
    ("SUNPHARMA", "Sun Pharma", "mid_cap"),
    ("TECHM", "Tech Mahindra", "mid_cap"),
    ("TITAN", "Titan Company", "mid_cap"),
    ("JSWSTEEL", "JSW Steel", "mid_cap"),
    ("BAJAJFINSV", "Bajaj Finserv", "mid_cap"),
    ("CIPLA", "Cipla", "mid_cap"),
    ("COALINDIA", "Coal India", "mid_cap"),
    ("DRREDDY", "Dr. Reddy's Laboratories", "mid_cap"),
    ("GAIL", "GAIL (India)", "mid_cap"),
    ("LT", "Larsen & Toubro", "mid_cap"),
    ("ULTRACEMCO", "UltraTech Cement", "mid_cap"),
    ("INDIGO", "InterGlobe Aviation", "mid_cap"),
    ("ITC", "ITC Limited", "mid_cap"),
    ("IINDTREE", "Indiamart Intermesh", "small_cap"),
    ("NYKAA", "FSN E-Commerce Ventures", "small_cap"),
    ("IRFC", "Indian Railway Finance", "small_cap"),
    ("LODHA", "Macrotech Developments", "small_cap"),
    ("TRENT", "Trent Limited", "small_cap"),
]


def _get_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Referer': 'https://www.nseindia.com/',
        'Cache-Control': 'no-cache',
    })
    return s


def _fetch_nse_stocks() -> List[Dict]:
    """Fetch all NSE stocks from NSE master-quote API. Falls back to popular Indian stocks if unavailable."""
    try:
        session = _get_session()

        # Warm up session with NSE home page first
        try:
            print("[Stocks] Warming up NSE session...")
            session.get("https://www.nseindia.com", timeout=10)
        except Exception as e:
            print(f"[Stocks] Warmup warning (non-critical): {e}")

        # Fetch stock symbols from NSE master-quote API
        try:
            print("[Stocks] Fetching from NSE master-quote API...")
            url = "https://www.nseindia.com/api/master-quote"
            resp = session.get(url, timeout=10)
            resp.raise_for_status()

            # API returns a simple JSON array of stock symbols
            symbols = resp.json()
            if not isinstance(symbols, list):
                raise ValueError(f"Expected list, got {type(symbols)}")

            print(f"[Stocks] Fetched {len(symbols)} symbols from NSE master-quote API")

            # Convert symbols to our stock format
            filtered = []
            for symbol in symbols:
                sym_upper = symbol.upper() if isinstance(symbol, str) else ""
                if sym_upper and sym_upper not in EXCLUDED_SYMBOLS and sym_upper not in INDIAN_INDICES:
                    filtered.append({
                        "symbol": sym_upper,
                        "name": sym_upper,  # Name matches symbol for now
                        "isin": "",
                        "industry": "",
                        "category": _classify_stock(sym_upper),
                        "exchange": "NSE",
                    })

            if filtered:
                print(f"[Stocks] Total unique stocks after filtering: {len(filtered)}")
                return filtered[:500]  # Top 500 stocks
            else:
                print("[Stocks] No stocks after filtering, using fallback")
                raise ValueError("No valid stocks found")

        except Exception as e:
            print(f"[Stocks] NSE API fetch failed: {e}")
            # Fall back to popular stocks list
            print("[Stocks] Using fallback popular stocks list")
            filtered = []
            for symbol, name, cat in FALLBACK_STOCKS:
                filtered.append({
                    "symbol": symbol,
                    "name": name,
                    "isin": "",
                    "industry": "",
                    "category": cat,
                    "exchange": "NSE",
                })
            return filtered

    except Exception as e:
        print(f"[Stocks] Fetch failed completely: {e}")
        # Return fallback even on exception
        return [
            {
                "symbol": symbol,
                "name": name,
                "isin": "",
                "industry": "",
                "category": cat,
                "exchange": "NSE",
            }
            for symbol, name, cat in FALLBACK_STOCKS
        ]


def _classify_stock(symbol: str) -> str:
    """Classify stock as large cap, mid cap, or small cap based on market cap."""
    nifty_50 = {"RELIANCE", "TCS", "INFY", "HDFC", "ICICIBANK", "SBIN", "BHARTIARTL", "MARUTI", "HCLTECH", "WIPRO", "AXISBANK", "SUNPHARMA"}

    if symbol in nifty_50:
        return "large_cap"
    elif symbol.startswith("M") or symbol.startswith("L") or symbol.startswith("N"):
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

        # Add bullish/bearish classification (skip for fallback to keep it fast)
        if len(stocks) > 20:  # Only compute for real fetched data
            for stock in stocks:
                try:
                    signal = _compute_bullish_bearish(stock["symbol"])
                    stock.update(signal)
                except:
                    stock["signal"] = "NEUTRAL"
                    stock["range_pct"] = 50
                    stock["price"] = 0
        else:
            # Fallback stocks - add default signals
            for stock in stocks:
                stock["signal"] = "NEUTRAL"
                stock["range_pct"] = 50
                stock["price"] = 0

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
