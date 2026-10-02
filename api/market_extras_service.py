"""
Mutual Fund Picks and IPO Calendar -- both backed by real, free, public data
sources with no API key required:

  - Mutual funds: AMFI's own NAV history via mfapi.in (a well-known free
    wrapper around AMFI's published NAV data), used to compute real 1/3/5-year
    CAGR -- never an invented or estimated return figure.
  - IPOs: NSE's own public IPO APIs (nseindia.com), the same exchange this
    app already sources its F&O bhavcopy from.

Honestly reports unavailable (never fabricates a fund/return/IPO) if a
source can't be reached.
"""
import time
import concurrent.futures
from datetime import datetime, timedelta
from curl_cffi import requests

from .nse_service import nse_service, now_ist_iso

MF_SCHEME_MASTER_TTL_SECONDS = 86400  # the scheme list itself barely changes
MF_PICKS_CACHE_TTL_SECONDS = 21600    # NAV is published once per trading day
IPO_CACHE_TTL_SECONDS = 1800

_mf_scheme_master_cache = {"time": 0.0, "data": None}
_mf_picks_cache = {"time": 0.0, "data": None}
_ipo_cache = {"time": 0.0, "data": None}

# A fixed, well-known-AMC x category universe (mirrors the Stock Picks
# approach of a bounded, deterministic universe rather than ranking all
# ~38,000 schemes on every request). "Best" here means "real 5-year CAGR,
# highest first" among this universe, not an endorsement.
MF_CATEGORIES = ["Flexi Cap", "Large Cap", "Mid Cap", "Small Cap", "ELSS"]
MF_TRUSTED_AMCS = [
    "HDFC", "SBI", "ICICI Prudential", "Axis", "Kotak", "Nippon India",
    "Aditya Birla Sun Life", "UTI", "DSP", "Mirae Asset", "Parag Parikh",
    "Quant", "Canara Robeco", "Motilal Oswal", "Franklin India", "Edelweiss",
    "Invesco India", "Tata", "Bandhan", "PGIM India",
]
MF_EXCLUDE_KEYWORDS = ["IDCW", "Dividend", "Bonus"]
MF_MAX_PER_CATEGORY = 8


def _get_mf_scheme_master():
    now = time.time()
    cached = _mf_scheme_master_cache["data"]
    if cached is not None and (now - _mf_scheme_master_cache["time"]) < MF_SCHEME_MASTER_TTL_SECONDS:
        return cached
    resp = requests.get("https://api.mfapi.in/mf", timeout=40, impersonate="chrome")
    resp.raise_for_status()
    data = resp.json()
    _mf_scheme_master_cache["time"] = now
    _mf_scheme_master_cache["data"] = data
    return data


def _select_mf_universe():
    schemes = _get_mf_scheme_master()
    selected = []
    for category in MF_CATEGORIES:
        count = 0
        for amc in MF_TRUSTED_AMCS:
            if count >= MF_MAX_PER_CATEGORY:
                break
            match = next(
                (s for s in schemes
                 if s["schemeName"].startswith(amc)
                 and "Direct" in s["schemeName"]
                 and "Growth" in s["schemeName"]
                 and category.lower() in s["schemeName"].lower()
                 and not any(x in s["schemeName"] for x in MF_EXCLUDE_KEYWORDS)),
                None,
            )
            if match:
                selected.append((match["schemeCode"], match["schemeName"], category))
                count += 1
    return selected


def _parse_mf_date(s):
    return datetime.strptime(s, "%d-%m-%Y")


def _compute_cagr(data, years):
    """Real CAGR from real NAV history -- returns None (not a guess) when
    there isn't enough history for this horizon."""
    if len(data) < 2:
        return None
    latest_date = _parse_mf_date(data[0]["date"])
    latest_nav = float(data[0]["nav"])
    target = latest_date - timedelta(days=365 * years)
    if _parse_mf_date(data[-1]["date"]) > target:
        return None
    closest = min(data, key=lambda r: abs((_parse_mf_date(r["date"]) - target).days))
    old_date = _parse_mf_date(closest["date"])
    old_nav = float(closest["nav"])
    actual_years = (latest_date - old_date).days / 365.25
    if old_nav <= 0 or actual_years <= 0:
        return None
    return round(((latest_nav / old_nav) ** (1 / actual_years) - 1) * 100, 2)


def _clean_fund_name(name):
    return (
        name.replace(" - Direct Plan - Growth Option", "")
            .replace(" - Direct Plan - Growth", "")
            .strip()
    )


def _fetch_one_fund(entry):
    scheme_code, scheme_name, category = entry
    try:
        resp = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=10, impersonate="chrome")
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or []
        if not data:
            return None
        return {
            "schemeCode": scheme_code,
            "name": _clean_fund_name(scheme_name),
            "fundHouse": (payload.get("meta") or {}).get("fund_house"),
            "category": category,
            "nav": float(data[0]["nav"]),
            "navDate": data[0]["date"],
            "cagr1y": _compute_cagr(data, 1),
            "cagr3y": _compute_cagr(data, 3),
            "cagr5y": _compute_cagr(data, 5),
        }
    except Exception:
        return None


def get_mutual_fund_picks(force_refresh=False):
    now = time.time()
    cached = _mf_picks_cache["data"]
    if not force_refresh and cached is not None and (now - _mf_picks_cache["time"]) < MF_PICKS_CACHE_TTL_SECONDS:
        return cached

    try:
        universe = _select_mf_universe()
    except Exception as e:
        return {
            "available": False,
            "reason": f"Could not load the mutual fund scheme list: {e}",
            "funds": [],
            "timestamp": now_ist_iso(),
        }

    if not universe:
        return {
            "available": False,
            "reason": "No matching mutual fund schemes found in the configured universe",
            "funds": [],
            "timestamp": now_ist_iso(),
        }

    funds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as pool:
        for result in pool.map(_fetch_one_fund, universe):
            if result and result["cagr5y"] is not None:
                funds.append(result)

    funds.sort(key=lambda f: f["cagr5y"], reverse=True)

    result = {
        "available": len(funds) > 0,
        "reason": None if funds else "Could not fetch usable NAV history for this fund universe right now",
        "universe": f"{len(universe)} Direct-Growth equity schemes across {len(MF_CATEGORIES)} categories from major AMCs, ranked by real 5-year CAGR",
        "count": len(funds),
        "funds": funds,
        "note": "These same open-ended schemes are what a SIP buys into -- a SIP is a way of investing in a fund, not a separate fund type. Past returns are not a guarantee of future performance.",
        "dataSource": "AMFI NAV history via mfapi.in",
        "timestamp": now_ist_iso(),
    }
    _mf_picks_cache["time"] = now
    _mf_picks_cache["data"] = result
    return result


# ---------------------------------------------------------------------------
# IPO Calendar -- real NSE data (upcoming / ongoing / recently closed)
# ---------------------------------------------------------------------------

def _fetch_nse_json(url):
    s = nse_service._get_session()
    nse_service.init_nse_session()
    r = s.get(url, headers={
        "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }, timeout=10)
    r.raise_for_status()
    return r.json()


def _clean_active_or_upcoming(x):
    return {
        "company": x.get("companyName"),
        "symbol": x.get("symbol"),
        "priceRange": x.get("issuePrice"),
        "startDate": x.get("issueStartDate"),
        "endDate": x.get("issueEndDate"),
        "series": x.get("series"),
        "status": x.get("status"),
    }


def _clean_closed(x):
    listing_date = x.get("listingDate")
    return {
        "company": x.get("company") or x.get("companyName"),
        "symbol": x.get("symbol"),
        "priceRange": x.get("priceRange") or x.get("issuePrice"),
        "startDate": x.get("ipoStartDate"),
        "endDate": x.get("ipoEndDate"),
        "listingDate": listing_date if listing_date not in (None, "-") else None,
        "series": x.get("securityType"),
    }


def get_ipo_data(force_refresh=False):
    now = time.time()
    cached = _ipo_cache["data"]
    if not force_refresh and cached is not None and (now - _ipo_cache["time"]) < IPO_CACHE_TTL_SECONDS:
        return cached

    try:
        raw = _fetch_nse_json("https://www.nseindia.com/api/all-upcoming-issues?category=ipo")
    except Exception as e:
        return {
            "available": False,
            "reason": f"NSE IPO data request failed: {e}",
            "ongoing": [], "upcoming": [], "closed": [],
            "timestamp": now_ist_iso(),
        }

    ongoing = [_clean_active_or_upcoming(x) for x in raw if x.get("status") == "Active"]
    upcoming = [_clean_active_or_upcoming(x) for x in raw if x.get("status") != "Active"]

    closed = []
    try:
        past_raw = _fetch_nse_json("https://www.nseindia.com/api/public-past-issues?category=ipo")
        closed = [_clean_closed(x) for x in past_raw[:25]]
    except Exception:
        pass  # closed history is a bonus -- ongoing/upcoming alone still count as available

    result = {
        "available": True,
        "ongoing": ongoing,
        "upcoming": upcoming,
        "closed": closed,
        "dataSource": "NSE (nseindia.com public IPO API)",
        "timestamp": now_ist_iso(),
    }
    _ipo_cache["time"] = now
    _ipo_cache["data"] = result
    return result
