"""Live market-news ticker feed, adapted from the RSS source catalog used by
github.com/krishnateja08/Global_Market_News — same public RSS feeds, fetched
and parsed directly here instead of depending on that repo's own generated
dashboard page (which only updates hourly and bakes everything into one big
static HTML file, not something meant to be embedded as a UI element). This
keeps a focused subset of its 14 categories — the ones that actually move an
NSE/BSE options market — rather than the full dashboard's global sweep.
"""

import concurrent.futures
import html
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import defusedxml.ElementTree as ET

REQUEST_TIMEOUT = 8
MAX_PER_FEED = 8
CACHE_TTL_SECONDS = 600  # a ticker doesn't need per-request freshness

RSS_SOURCES = {
    "india": [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://economictimes.indiatimes.com/rssfeeds/1373380680.cms",
        "https://news.google.com/rss/search?q=NSE+BSE+Nifty+Sensex+stock+market+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    ],
    "markets": [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC,^DJI,^IXIC&region=US&lang=en-US",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
    ],
    "macro_policy": [
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "https://news.google.com/rss/search?q=trade+tariffs+supply+chain+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
    "banking_fx": [
        "https://www.cnbc.com/id/10000108/device/rss/rss.html",
        "https://news.google.com/rss/search?q=Fed+RBI+ECB+interest+rate+decision+when:2d&hl=en-US&gl=US&ceid=US:en",
    ],
    "commodities_energy": [
        "https://www.cnbc.com/id/10000113/device/rss/rss.html",
        "https://news.google.com/rss/search?q=OPEC+oil+gold+energy+when:1d&hl=en-US&gl=US&ceid=US:en",
    ],
    "crypto": [
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ],
}

SOURCE_DOMAIN_NAME = {
    "cnbc.com": "CNBC",
    "marketwatch.com": "MarketWatch",
    "finance.yahoo.com": "Yahoo Finance",
    "economictimes.indiatimes.com": "Economic Times",
    "moneycontrol.com": "MoneyControl",
    "news.google.com": "Google News",
    "cointelegraph.com": "CoinTelegraph",
    "coindesk.com": "CoinDesk",
}

_cache_lock = threading.Lock()
_cache = {"items": [], "fetchedAt": 0.0}


def _source_name(url):
    domain = urllib.parse.urlparse(url).netloc.replace("www.", "").replace("feeds.", "")
    return next((v for k, v in SOURCE_DOMAIN_NAME.items() if k in domain), domain)


def _parse_pub_date(raw):
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def _fetch_feed(url, category):
    items = []
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; RajanTradingNewsTicker/1.0)",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Strip namespaces — several of these feeds mix in media:/dc: prefixes
        # that ElementTree would otherwise require fully-qualified tag lookups for.
        raw = re.sub(r'\s+xmlns[^"]*"[^"]*"', "", raw)
        raw = re.sub(r"<(/?)[\w]+:", r"<\1", raw)

        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is None:
            channel = root

        source = _source_name(url)
        for item in channel.findall("item")[:MAX_PER_FEED]:
            title = html.unescape((item.findtext("title") or "").strip())
            title = title.replace("�", "").strip()  # strip decode-artifact replacement chars
            link = (item.findtext("link") or "").strip()
            pub_date_raw = (item.findtext("pubDate") or item.findtext("date") or "").strip()
            if title and len(title) > 10:
                items.append({
                    "title": title,
                    "link": link,
                    "source": source,
                    "category": category,
                    "pubDate": pub_date_raw,
                    "pubDateParsed": _parse_pub_date(pub_date_raw),
                })
    except Exception:
        pass
    return items


def _fetch_all_live(max_workers=16):
    tasks = [(url, cat) for cat, urls in RSS_SOURCES.items() for url in urls]
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_fetch_feed, url, cat) for url, cat in tasks]
        for fut in concurrent.futures.as_completed(futures):
            all_items.extend(fut.result())

    seen_titles = set()
    deduped = []
    for it in all_items:
        key = it["title"].lower()[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(it)

    fallback_dt = datetime.utcnow() - timedelta(hours=48)
    deduped.sort(key=lambda it: it["pubDateParsed"] or fallback_dt, reverse=True)
    for it in deduped:
        it.pop("pubDateParsed", None)

    return deduped


def get_market_news(limit=40, force_refresh=False):
    with _cache_lock:
        age = time.time() - _cache["fetchedAt"]
        if not force_refresh and _cache["items"] and age < CACHE_TTL_SECONDS:
            return {"items": _cache["items"][:limit], "fetchedAt": _cache["fetchedAt"], "cached": True}

    items = _fetch_all_live()
    with _cache_lock:
        if items:
            _cache["items"] = items
            _cache["fetchedAt"] = time.time()
        elif not _cache["items"]:
            _cache["fetchedAt"] = time.time()
        result_items = _cache["items"][:limit]
        fetched_at = _cache["fetchedAt"]

    return {"items": result_items, "fetchedAt": fetched_at, "cached": False}
