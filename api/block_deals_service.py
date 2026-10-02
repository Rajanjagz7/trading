"""Block deals tracker — fetches daily block deals from NSE public data."""

import time
import requests
from datetime import datetime
from typing import Optional, List, Dict

BLOCK_DEALS_CACHE_TTL = 1800  # 30 minutes
_cache = {"time": 0.0, "data": None}
_cache_lock = None


def _get_session():
    """Get a requests session with proper NSE headers."""
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': 'https://www.nseindia.com/',
    })
    return s


def _fetch_block_deals(session: requests.Session) -> List[Dict]:
    """Fetch today's block deals from NSE."""
    try:
        # NSE block deals API endpoint
        url = "https://www.nseindia.com/api/blockDeal"

        # First request to set cookies
        session.get("https://www.nseindia.com/market-data/block-deals", timeout=10)

        # Fetch block deals data
        resp = session.get(url, timeout=10)
        resp.raise_for_status()

        data = resp.json()
        if isinstance(data, dict) and 'data' in data:
            deals = data['data']
        else:
            deals = data if isinstance(data, list) else []

        # Clean and format the data
        formatted_deals = []
        for deal in deals:
            try:
                formatted_deals.append({
                    'symbol': deal.get('Symbol', '').upper(),
                    'quantity': int(deal.get('Quantity', 0)),
                    'price': float(deal.get('Price', 0)),
                    'value': float(deal.get('Value', 0)),
                    'time': deal.get('Time', ''),
                    'clientName': deal.get('ClientName', 'N/A'),
                    'dealType': deal.get('DealType', 'N/A'),
                })
            except (ValueError, KeyError, TypeError):
                continue

        return formatted_deals

    except Exception as e:
        print(f"[Block Deals] Fetch failed: {e}")
        return []


def get_block_deals(force_refresh: bool = False) -> Dict:
    """Get today's block deals with caching."""
    global _cache

    now = time.time()
    age = now - _cache["time"]

    # Return cached data if fresh
    if not force_refresh and _cache["data"] is not None and age < BLOCK_DEALS_CACHE_TTL:
        return {
            "available": True,
            "deals": _cache["data"],
            "count": len(_cache["data"]),
            "fetchedAt": datetime.now().isoformat(),
            "cached": True,
            "dataSource": "NSE Block Deals API",
        }

    # Fetch fresh data
    session = _get_session()
    deals = _fetch_block_deals(session)

    # Update cache
    _cache["time"] = now
    _cache["data"] = deals

    # Group by symbol for easy filtering
    by_symbol = {}
    for deal in deals:
        sym = deal['symbol']
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(deal)

    return {
        "available": len(deals) > 0,
        "deals": deals,
        "bySymbol": by_symbol,
        "count": len(deals),
        "uniqueSymbols": len(by_symbol),
        "fetchedAt": datetime.now().isoformat(),
        "cached": False,
        "dataSource": "NSE Block Deals API",
    }
