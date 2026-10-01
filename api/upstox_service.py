import os
import math
import time
from datetime import datetime, date, timedelta
from urllib.parse import quote
from curl_cffi import requests

def _load_env_file():
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env.local"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.getcwd(), ".env.local"),
        ".env",
        ".env.local"
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'").strip('"')
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env_file()


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
        "NIFTY 50": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "NIFTY BANK": "NSE_INDEX|Nifty Bank",
        "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
        "NIFTY FIN SERVICE": "NSE_INDEX|Nifty Fin Service",
        "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
        "NIFTY MIDCAP": "NSE_INDEX|NIFTY MID SELECT",
        "NIFTY MID SELECT": "NSE_INDEX|NIFTY MID SELECT",
        "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
        "SENSEX": "BSE_INDEX|SENSEX",
        "INDIA VIX": "NSE_INDEX|India VIX",
        "INDIAVIX": "NSE_INDEX|India VIX",
        "VIX": "NSE_INDEX|India VIX",
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

        spot_val = 0.0
        for r in rows:
            if r.get("spot") and r["spot"] > 0:
                spot_val = r["spot"]
                break

        result = {
            "symbol": symbol,
            "spot": spot_val,
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

    def _parse_candles(self, payload):
        """Upstox returns candles most-recent-first as
        [timestamp, open, high, low, close, volume, oi]; normalize to
        chronological order with named fields. No fabricated values --
        anything not returned by the API is simply absent here."""
        raw = (payload.get("data") or {}).get("candles") or []
        out = []
        for c in raw:
            out.append({
                "time": c[0],
                "open": self._num(c[1]),
                "high": self._num(c[2]),
                "low": self._num(c[3]),
                "close": self._num(c[4]),
                "volume": self._num(c[5]) if len(c) > 5 else 0.0,
                "oi": self._num(c[6]) if len(c) > 6 else 0.0,
            })
        out.reverse()
        return out

    def fetch_intraday_candles(self, instrument_key, unit="minutes", interval="5"):
        """Today's real intraday candles from Upstox's V3 intraday endpoint."""
        encoded_key = quote(instrument_key, safe="")
        url = f"{self.BASE_V3}/historical-candle/intraday/{encoded_key}/{unit}/{interval}"
        return self._parse_candles(self._get(url))

    def fetch_historical_candles(self, instrument_key, unit="minutes", interval="30", to_date=None, from_date=None):
        """Real OHLCV candles over a date range from Upstox's V3 historical-candle endpoint."""
        encoded_key = quote(instrument_key, safe="")
        url = f"{self.BASE_V3}/historical-candle/{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"
        return self._parse_candles(self._get(url))

    def get_candles(self, instrument_key, timeframe="5minute"):
        """
        Real candle series for a chart -- no synthetic/estimated points.
        timeframe: "1minute" | "5minute" | "15minute" | "30minute" | "day"
        Intraday timeframes try today's live intraday candles first, then
        fall back to the most recent trading day's historical candles
        (markets closed / weekend) so the chart still shows something real
        rather than going blank.
        """
        unit_map = {
            "1minute": ("minutes", "1"), "5minute": ("minutes", "5"),
            "15minute": ("minutes", "15"), "30minute": ("minutes", "30"),
            "day": ("days", "1"),
        }
        unit, interval = unit_map.get(timeframe, ("minutes", "5"))

        if timeframe == "day":
            to_d = date.today().isoformat()
            from_d = (date.today() - timedelta(days=90)).isoformat()
            return self.fetch_historical_candles(instrument_key, unit, interval, to_d, from_d)

        try:
            candles = self.fetch_intraday_candles(instrument_key, unit, interval)
            if candles:
                return candles
        except Exception as e:
            print(f"[Upstox] intraday candle fetch failed for {instrument_key}: {e}")

        try:
            to_d = date.today().isoformat()
            from_d = (date.today() - timedelta(days=7)).isoformat()
            return self.fetch_historical_candles(instrument_key, unit, interval, to_d, from_d)
        except Exception as e:
            print(f"[Upstox] historical candle fallback failed for {instrument_key}: {e}")
            return []

    def get_all_indices_quotes(self):
        """
        Fetches real-time market quotes for all major indices:
        NIFTY, BANKNIFTY, SENSEX, INDIA VIX, FINNIFTY, MIDCPNIFTY.
        Cached for 3 seconds to avoid duplicate REST requests.
        """
        if not self.is_configured():
            return {}

        now = time.time()
        cached = self.cache.get("ALL_INDICES_QUOTES")
        if cached and now - cached[0] < 3:
            return cached[1]

        keys_map = {
            "NIFTY": "NSE_INDEX|Nifty 50",
            "BANKNIFTY": "NSE_INDEX|Nifty Bank",
            "SENSEX": "BSE_INDEX|SENSEX",
            "VIX": "NSE_INDEX|India VIX",
            "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
            "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
        }

        try:
            url = f"{self.BASE_V2}/market-quote/quotes"
            params = {"instrument_key": ",".join(keys_map.values())}
            res = self._get(url, params=params, timeout=8)
            raw_data = res.get("data", {})

            quotes = {}
            for sym, ikey in keys_map.items():
                quote_obj = raw_data.get(ikey)
                if not quote_obj:
                    colon_key = ikey.replace("|", ":")
                    quote_obj = raw_data.get(colon_key)

                if quote_obj:
                    last = self._num(quote_obj.get("last_price"))
                    ohlc = quote_obj.get("ohlc") or {}
                    close = self._num(ohlc.get("close"), last)
                    change = self._num(quote_obj.get("net_change"))
                    if change == 0.0 and close > 0 and last > 0:
                        change = round(last - close, 2)
                    prev_close = last - change if last > 0 else close
                    pct = round((change / prev_close) * 100.0, 2) if prev_close > 0 else 0.0

                    quotes[sym] = {
                        "symbol": sym,
                        "last": last,
                        "change": change,
                        "pct": pct,
                        "open": self._num(ohlc.get("open")),
                        "high": self._num(ohlc.get("high")),
                        "low": self._num(ohlc.get("low")),
                        "close": close,
                    }

            self.cache["ALL_INDICES_QUOTES"] = (now, quotes)
            return quotes
        except Exception as e:
            print(f"[Upstox] get_all_indices_quotes error: {e}")
            return {}

    def health(self):
        if not self.is_configured():
            return {
                "configured": False,
                "provider": "Upstox",
                "mode": "REST",
            }
        try:
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
