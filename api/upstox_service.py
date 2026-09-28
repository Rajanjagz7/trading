import os
import math
import time
from datetime import datetime, date
from urllib.parse import quote
from curl_cffi import requests


class UpstoxService:
    """
    REST-only Upstox integration for the existing scanner.

    Uses:
      - Instrument Search / Option Contracts to resolve the underlying
      - Put/Call Option Chain for contracts + LTP/OI/volume
      - Full Market Quotes V3 for live OHLC/previous close

    This deliberately does not create a persistent WebSocket because the
    current deployment is Vercel serverless. The existing frontend polling
    interval therefore becomes the live refresh cadence.
    """

    BASE_V2 = "https://api.upstox.com/v2"
    BASE_V3 = "https://api.upstox.com/v3"

    INDEX_KEYS = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
        "MIDCPNIFTY": "NSE_INDEX|Nifty Midcap Select",
        "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
    }

    def __init__(self):
        self.session = requests.Session(impersonate="chrome")
        self.cache = {}
        self.instrument_cache = {}

    def is_configured(self):
        return bool(os.getenv("UPSTOX_ACCESS_TOKEN"))

    def _headers(self):
        token = os.getenv("UPSTOX_ACCESS_TOKEN")
        if not token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is not configured")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _get(self, url, params=None, timeout=12):
        response = self.session.get(
            url,
            params=params,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code >= 400:
            body = response.text[:1000]
            raise RuntimeError(
                f"Upstox API HTTP {response.status_code}: {body}"
            )
        payload = response.json()
        if payload.get("status") not in (None, "success"):
            raise RuntimeError(f"Upstox API error: {payload}")
        return payload

    def _resolve_underlying(self, symbol):
        symbol = symbol.upper().strip()
        if symbol in self.INDEX_KEYS:
            return self.INDEX_KEYS[symbol], True

        if symbol in self.instrument_cache:
            return self.instrument_cache[symbol], False

        payload = self._get(
            f"{self.BASE_V2}/instruments/search",
            params={
                "query": symbol,
                "exchanges": "NSE",
                "segments": "FO",
                "instrument_types": "CE,PE",
                "expiry": "current_month",
                "page_number": 1,
                "records": 30,
            },
        )
        rows = payload.get("data", [])
        candidates = [
            x for x in rows
            if str(x.get("underlying_symbol", "")).upper() == symbol
            and x.get("underlying_key")
        ]
        if not candidates:
            raise RuntimeError(
                f"Could not resolve Upstox underlying instrument for {symbol}"
            )

        key = candidates[0]["underlying_key"]
        self.instrument_cache[symbol] = key
        return key, False

    @staticmethod
    def _normal_expiry(value):
        if not value:
            return None
        return str(value)[:10]

    def _get_contracts(self, underlying_key, expiry=None):
        params = {"instrument_key": underlying_key}
        if expiry:
            params["expiry_date"] = expiry
        payload = self._get(
            f"{self.BASE_V2}/option/contract",
            params=params,
        )
        return payload.get("data", [])

    def _resolve_expiry(self, contracts, requested):
        expiries = sorted({
            self._normal_expiry(x.get("expiry"))
            for x in contracts
            if self._normal_expiry(x.get("expiry"))
        })
        today = date.today().isoformat()
        future = [x for x in expiries if x >= today]
        if requested and requested in expiries:
            target = requested
        elif future:
            target = future[0]
        elif expiries:
            target = expiries[0]
        else:
            raise RuntimeError("Upstox returned no option expiry dates")
        return target, expiries

    def _full_quotes(self, instrument_keys):
        result = {}
        # Upstox limits one full-quote request to 500 instruments.
        for start in range(0, len(instrument_keys), 500):
            batch = instrument_keys[start:start + 500]
            payload = self._get(
                f"{self.BASE_V3}/market-quote/quotes",
                params={"instrument_key": ",".join(batch)},
                timeout=15,
            )
            for key, quote in (payload.get("data") or {}).items():
                instrument_token = quote.get("instrument_token")
                result[instrument_token or key] = quote
        return result

    @staticmethod
    def _num(value, default=0.0):
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def fetch_option_chain(self, symbol="NIFTY", expiry=None):
        symbol = symbol.upper().strip()
        cache_key = f"{symbol}|{expiry or ''}"
        now = time.time()

        # Keep a very short cache so two simultaneous browser requests don't
        # duplicate expensive Upstox REST calls.
        cached = self.cache.get(cache_key)
        if cached and now - cached[0] < 3:
            return cached[1]

        underlying_key, is_index = self._resolve_underlying(symbol)

        contracts = self._get_contracts(underlying_key)
        target_expiry, expiry_dates = self._resolve_expiry(contracts, expiry)

        chain = self._get(
            f"{self.BASE_V2}/option/chain",
            params={
                "instrument_key": underlying_key,
                "expiry_date": target_expiry,
            },
        )
        chain_rows = chain.get("data", [])

        instrument_keys = []
        for item in chain_rows:
            for side in ("call_options", "put_options"):
                option = item.get(side) or {}
                key = option.get("instrument_key")
                if key:
                    instrument_keys.append(key)

        quotes = self._full_quotes(list(dict.fromkeys(instrument_keys)))

        rows = []
        for item in chain_rows:
            strike = self._num(item.get("strike_price"))
            spot = self._num(item.get("underlying_spot_price"))

            for opt_type, field in (("CE", "call_options"), ("PE", "put_options")):
                option = item.get(field) or {}
                key = option.get("instrument_key")
                if not key:
                    continue

                market = option.get("market_data") or {}
                quote = quotes.get(key) or quotes.get(
                    option.get("instrument_token")
                ) or {}

                ohlc = quote.get("ohlc") or {}
                ltp = self._num(
                    quote.get("last_price"),
                    self._num(market.get("ltp")),
                )
                prev_close = self._num(
                    quote.get("prev_close_price"),
                    self._num(market.get("close_price")),
                )
                open_price = self._num(ohlc.get("open"))
                high_price = self._num(ohlc.get("high"))
                low_price = self._num(ohlc.get("low"))
                volume = self._num(
                    quote.get("volume"),
                    self._num(market.get("volume")),
                )
                oi = self._num(
                    quote.get("oi"),
                    self._num(market.get("oi")),
                )
                previous_oi = self._num(
                    quote.get("previous_oi"),
                    self._num(market.get("prev_oi")),
                )
                oi_change = oi - previous_oi if previous_oi else 0.0

                rows.append({
                    "symbol": symbol,
                    "instr": "Index" if is_index else "Stock",
                    "expiry": self._normal_expiry(item.get("expiry")) or target_expiry,
                    "strike": strike,
                    "type": opt_type,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": ltp,
                    "prevClose": prev_close,
                    "spot": spot,
                    "oi": oi,
                    "oiChgRaw": oi_change,
                    "vol": volume,
                    "bid": self._num(market.get("bid_price")),
                    "ask": self._num(market.get("ask_price")),
                    "instrumentKey": key,
                    "source": "Upstox",
                    "lastTradeTime": quote.get("last_trade_time"),
                })

        if not rows:
            raise RuntimeError(
                f"Upstox returned no option-chain contracts for {symbol} {target_expiry}"
            )

        result = {
            "symbol": symbol,
            "targetExpiry": target_expiry,
            "expiryDates": expiry_dates,
            "rows": rows,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "provider": "Upstox",
            "mode": "REST",
            "underlyingKey": underlying_key,
        }
        self.cache[cache_key] = (now, result)
        return result

    def health(self):
        if not self.is_configured():
            return {
                "configured": False,
                "provider": "Upstox",
                "mode": "REST",
            }
        try:
            # Lightweight authenticated request. Instrument search is used
            # because it also confirms the token has market-data access.
            key, is_index = self._resolve_underlying("NIFTY")
            return {
                "configured": True,
                "provider": "Upstox",
                "mode": "REST",
                "connected": True,
                "underlyingKey": key,
            }
        except Exception as exc:
            return {
                "configured": True,
                "provider": "Upstox",
                "mode": "REST",
                "connected": False,
                "error": str(exc),
            }


upstox_service = UpstoxService()
