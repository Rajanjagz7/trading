import io
import os
import csv
import json
import time
import zipfile
import concurrent.futures
from datetime import datetime, timezone, timedelta
from curl_cffi import requests
from .upstox_service import upstox_service

# Fixed UTC+5:30 offset rather than zoneinfo's "Asia/Kolkata" -- India has no
# DST so the offset never changes, and this avoids a hard dependency on the
# host OS having IANA tzdata installed (not guaranteed on every runtime).
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist_iso():
    return datetime.now(IST).isoformat()


def validate_ohlc(open_, high, low, close):
    """
    Spec section 10: basic OHLC consistency validation.
    Returns (is_valid, violations) -- violations is a list of human-readable
    rule names that failed, empty when all pass. Pure function, no I/O, so
    it's directly unit-testable and reusable anywhere a contract/candle's
    OHLC needs a sanity check before being trusted.
    """
    violations = []
    if high < open_:
        violations.append("High < Open")
    if high < close:
        violations.append("High < Close")
    if high < low:
        violations.append("High < Low")
    if low > open_:
        violations.append("Low > Open")
    if low > close:
        violations.append("Low > Close")
    return (len(violations) == 0, violations)


def compute_recommendation(sym, verdict, top_call, top_put, data_status):
    """
    "Recommendation for the Day" -- a documented, deterministic, reproducible
    rule, not a separate guess: direction comes straight from the same
    Call/Put volume-share verdict already computed in get_indices_pulse()
    (>=55% one-sided = a signal, otherwise NO SIGNAL), and Entry/Target/SL
    reuse the exact +9% / +18% / -5% formulas already shown elsewhere in
    this app, applied to the real LTP of whichever contract (topCall or
    topPut) that verdict points at. No signal is produced when data is
    unavailable or the required contract is missing -- see the pytest
    suite (tests/test_market_data.py) for the exact input/output pairs
    this rule is pinned to.
    """
    if data_status == "unavailable":
        return None

    direction, basis = None, None
    if "BUY" in verdict and top_call:
        direction, basis = ("BUY CALL", top_call)
    elif "SELL" in verdict and top_put:
        direction, basis = ("BUY PUT", top_put)

    if direction and basis and basis.get("close", 0) > 0:
        entry = basis["close"]
        return {
            "instrument": sym,
            "contract": f"{sym} {basis['strike']} {'CE' if direction == 'BUY CALL' else 'PE'}",
            "signal": direction,
            "entry": round(entry, 2),
            "target1": round(entry * 1.09, 2),
            "target2": round(entry * 1.18, 2),
            "stopLoss": round(entry * 0.95, 2),
            "changePct": basis.get("chgPct", 0.0),
            "basedOnVerdict": verdict,
            "dataStatus": data_status,
            "timestamp": now_ist_iso(),
        }

    verdict_is_directional = ("BUY" in verdict) or ("SELL" in verdict)
    return {
        "instrument": sym,
        "signal": "NO SIGNAL",
        "reason": "Required option contract data is missing or stale" if verdict_is_directional
                  else "Call/Put flow is balanced (rangebound) -- no one-sided signal to act on",
        "dataStatus": data_status,
        "timestamp": now_ist_iso(),
    }

# SENSEX and India VIX have no NSE bhavcopy/F&O source at all (SENSEX is a BSE
# index, and BSE's own API hard-blocks this app's requests) — Yahoo Finance's
# public chart API gives a plain index quote for both with no auth needed.
YAHOO_INDEX_SYMBOL = {
    "SENSEX": "^BSESN",
    "VIX": "^INDIAVIX",
}

# Bundled data cache (read-only on Vercel)
STATIC_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data_cache")

# Writable cache (works on local machine and in Vercel /tmp)
WRITABLE_CACHE_DIR = "/tmp/data_cache" if os.environ.get("VERCEL") else STATIC_CACHE_DIR
try:
    os.makedirs(WRITABLE_CACHE_DIR, exist_ok=True)
except Exception:
    pass

class NSEService:
    def __init__(self):
        self.session = None
        self.today_rows = []
        self.prev_map = {}
        self.today_date = None
        self.prev_date = None
        self.last_fetch_time = None
        self.live_cache = {}
        # Pre-load bundled cache if available
        self._load_bundled_cache()

    def _get_session(self):
        if self.session is None:
            self.session = requests.Session(impersonate="chrome")
        return self.session

    def _load_bundled_cache(self):
        """Auto-load bundled CSVs on startup without needing network."""
        for cdir in [STATIC_CACHE_DIR, WRITABLE_CACHE_DIR]:
            if not os.path.exists(cdir):
                continue
            files = sorted(os.listdir(cdir), reverse=True)
            csvs = [f for f in files if f.startswith("BhavCopy_NSE_FO_") and f.endswith(".csv")]
            if csvs and not self.today_rows:
                try:
                    today_file = os.path.join(cdir, csvs[0])
                    with open(today_file, "r", encoding="utf-8") as f:
                        self.today_rows = self._parse_csv_text(f.read())
                    # Extract date from filename, e.g. BhavCopy_NSE_FO_0_0_0_20260925_F_0000.csv
                    parts = csvs[0].split("_")
                    if len(parts) >= 8 and len(parts[6]) == 8:
                        dstr = parts[6]
                        self.today_date = f"{dstr[6:8]}-{dstr[4:6]}-{dstr[0:4]}"
                    else:
                        self.today_date = "Cached Data"

                    if len(csvs) > 1:
                        prev_file = os.path.join(cdir, csvs[1])
                        with open(prev_file, "r", encoding="utf-8") as f:
                            prows = self._parse_csv_text(f.read())
                        self.prev_map = {f"{r['symbol']}|{r['expiry']}|{r['type']}|{r['strike']}": r for r in prows}
                        pparts = csvs[1].split("_")
                        if len(pparts) >= 8 and len(pparts[6]) == 8:
                            pdstr = pparts[6]
                            self.prev_date = f"{pdstr[6:8]}-{pdstr[4:6]}-{pdstr[0:4]}"
                    print(f"[CACHE INIT] Loaded {len(self.today_rows)} contracts for {self.today_date}")
                except Exception as e:
                    print(f"[CACHE INIT] Notice: {e}")

    def init_nse_session(self):
        s = self._get_session()
        try:
            s.get("https://www.nseindia.com", headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }, timeout=8)
            s.get("https://www.nseindia.com/all-reports", headers={
                "Referer": "https://www.nseindia.com/",
                "Accept": "text/html,*/*",
            }, timeout=8)
        except Exception as e:
            print(f"[NSE] Session init error: {e}")

    def _fetch_nse_market_status(self):
        s = self._get_session()
        try:
            r = s.get("https://www.nseindia.com/api/marketStatus", headers={
                "Referer": "https://www.nseindia.com/",
                "Accept": "application/json, text/javascript, */*; q=0.01"
            }, timeout=6)
            if r.status_code == 200:
                data = r.json()
                cm = next((m for m in data.get("marketState", []) if m.get("market") == "Capital Market"), {})
                return {
                    "marketStatus": cm.get("marketStatus", "Unknown"),
                    "tradeDate": cm.get("tradeDate", ""),
                    "niftyLast": cm.get("last", 0),
                    "niftyChange": cm.get("variation", 0),
                    "niftyPct": cm.get("percentChange", 0),
                }
        except Exception as e:
            print(f"[NSE] marketStatus error: {e}")
        return {"marketStatus": "Close", "tradeDate": self.today_date or ""}

    def _fetch_yahoo_index_quote(self, key):
        """Last close + change for SENSEX/VIX via Yahoo's public chart API —
        neither has any NSE bhavcopy source, and BSE's own API blocks this app."""
        yahoo_symbol = YAHOO_INDEX_SYMBOL[key]
        try:
            s = self._get_session()
            r = s.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
                params={"range": "5d", "interval": "1d"},
                timeout=5,
            )
            if r.status_code != 200:
                return None
            result = r.json()["chart"]["result"][0]
            meta = result.get("meta", {})
            last = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if not last:
                return None
            change = round(last - prev, 2) if prev else 0.0
            pct = round((change / prev) * 100.0, 2) if prev else 0.0
            return {"last": round(last, 2), "change": change, "pct": pct}
        except Exception as e:
            print(f"[Yahoo] {key} quote error: {e}")
            return None

    def get_market_status(self):
        # Run NSE's status call and the two Yahoo quotes concurrently — none of
        # this should make page-load status noticeably slower than fetching
        # NIFTY's status alone did before.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_nse = ex.submit(self._fetch_nse_market_status)
            f_sensex = ex.submit(self._fetch_yahoo_index_quote, "SENSEX")
            f_vix = ex.submit(self._fetch_yahoo_index_quote, "VIX")
            nse_status = f_nse.result()
            sensex = f_sensex.result()
            vix = f_vix.result()

        status = {
            **nse_status,
            "todayDateLoaded": self.today_date,
            "prevDateLoaded": self.prev_date,
            "totalLoaded": len(self.today_rows),
            "version": "2.2.0",
            "buildTime": "2026-09-27T07:30:00Z",
            "indices": {}
        }
        if sensex:
            status["sensexLast"] = sensex["last"]
            status["sensexChange"] = sensex["change"]
            status["sensexPct"] = sensex["pct"]
        if vix:
            status["vixLast"] = vix["last"]
            status["vixChange"] = vix["change"]
            status["vixPct"] = vix["pct"]

        # If Upstox is configured, enrich all index tickers with real-time live quotes
        if upstox_service.is_configured():
            try:
                up_quotes = upstox_service.get_all_indices_quotes()
                if up_quotes:
                    status["indices"] = up_quotes
                    if "NIFTY" in up_quotes:
                        status["niftyLast"] = up_quotes["NIFTY"]["last"]
                        status["niftyChange"] = up_quotes["NIFTY"]["change"]
                        status["niftyPct"] = up_quotes["NIFTY"]["pct"]
                    if "BANKNIFTY" in up_quotes:
                        status["bankLast"] = up_quotes["BANKNIFTY"]["last"]
                        status["bankChange"] = up_quotes["BANKNIFTY"]["change"]
                        status["bankPct"] = up_quotes["BANKNIFTY"]["pct"]
                    if "SENSEX" in up_quotes:
                        status["sensexLast"] = up_quotes["SENSEX"]["last"]
                        status["sensexChange"] = up_quotes["SENSEX"]["change"]
                        status["sensexPct"] = up_quotes["SENSEX"]["pct"]
                    if "VIX" in up_quotes:
                        status["vixLast"] = up_quotes["VIX"]["last"]
                        status["vixChange"] = up_quotes["VIX"]["change"]
                        status["vixPct"] = up_quotes["VIX"]["pct"]
                    if "MIDCPNIFTY" in up_quotes:
                        status["midcapLast"] = up_quotes["MIDCPNIFTY"]["last"]
                        status["midcapChange"] = up_quotes["MIDCPNIFTY"]["change"]
                        status["midcapPct"] = up_quotes["MIDCPNIFTY"]["pct"]
                    if "FINNIFTY" in up_quotes:
                        status["finLast"] = up_quotes["FINNIFTY"]["last"]
                        status["finChange"] = up_quotes["FINNIFTY"]["change"]
                        status["finPct"] = up_quotes["FINNIFTY"]["pct"]
            except Exception as e:
                print(f"[Upstox] get_market_status quote enrich error: {e}")

        return status

    def fetch_latest_bhavcopy(self, force_refresh=False):
        s = self._get_session()
        self.init_nse_session()

        r = s.get("https://www.nseindia.com/api/daily-reports?key=FO", headers={
            "Referer": "https://www.nseindia.com/all-reports",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }, timeout=12)
        
        if r.status_code != 200:
            if self.today_rows:
                return {
                    "success": True,
                    "todayDate": self.today_date,
                    "prevDate": self.prev_date,
                    "totalContracts": len(self.today_rows),
                    "message": "Using cached Bhavcopy",
                }
            raise RuntimeError(f"Failed to query NSE reports API: HTTP {r.status_code}")

        reports = r.json()
        curr_date = reports.get("currentDate")
        prev_date = reports.get("previousDate")

        curr_file_info = next((i for i in reports.get("CurrentDay", []) if i.get("fileKey") == "FO-UDIFF-BHAVCOPY-CSV"), None)
        prev_file_info = next((i for i in reports.get("PreviousDay", []) if i.get("fileKey") == "FO-UDIFF-BHAVCOPY-CSV"), None)

        if not curr_file_info and self.today_rows:
            return {"success": True, "todayDate": self.today_date, "totalContracts": len(self.today_rows)}

        curr_rows = self._load_or_download_zip(curr_file_info, force_refresh)
        prev_rows = self._load_or_download_zip(prev_file_info, force_refresh) if prev_file_info else []

        if curr_rows:
            self.today_rows = curr_rows
            self.today_date = curr_date
            self.prev_date = prev_date
            self.prev_map = {f"{r['symbol']}|{r['expiry']}|{r['type']}|{r['strike']}": r for r in prev_rows}
            self.last_fetch_time = datetime.now().isoformat()

        return {
            "success": True,
            "todayDate": self.today_date,
            "prevDate": self.prev_date,
            "totalContracts": len(self.today_rows),
            "prevContracts": len(prev_rows),
            "lastFetchTime": self.last_fetch_time,
        }

    def _load_or_download_zip(self, file_info, force_refresh=False):
        if not file_info:
            return []
        
        file_name = file_info.get("fileActlName")
        clean_name = file_name.replace(".zip", "")
        
        # Check static cache or writable cache
        for check_dir in [WRITABLE_CACHE_DIR, STATIC_CACHE_DIR]:
            cache_path = os.path.join(check_dir, clean_name)
            if not force_refresh and os.path.exists(cache_path):
                print(f"[CACHE] Loading cached file: {cache_path}")
                with open(cache_path, "r", encoding="utf-8") as f:
                    return self._parse_csv_text(f.read())

        url = file_info.get("filePath") + file_name
        print(f"[NSE] Downloading: {url}")
        s = self._get_session()
        res = s.get(url, headers={"Referer": "https://www.nseindia.com/all-reports"}, timeout=20)
        if res.status_code != 200:
            raise RuntimeError(f"Failed to download {file_name}: HTTP {res.status_code}")

        z = zipfile.ZipFile(io.BytesIO(res.content))
        csv_filename = z.namelist()[0]
        with z.open(csv_filename) as f:
            csv_text = f.read().decode("utf-8")

        try:
            write_path = os.path.join(WRITABLE_CACHE_DIR, clean_name)
            with open(write_path, "w", encoding="utf-8") as f:
                f.write(csv_text)
        except Exception as e:
            print(f"[CACHE] Write notice: {e}")

        return self._parse_csv_text(csv_text)

    def _parse_csv_text(self, csv_text):
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = []
        for r in reader:
            opt_type = r.get("OptnTp")
            if opt_type in ("CE", "PE"):
                try:
                    rows.append({
                        "symbol": r.get("TckrSymb", ""),
                        "instr": "Index" if r.get("FinInstrmTp") == "IDO" else "Stock",
                        "expiry": r.get("XpryDt", ""),
                        "strike": float(r.get("StrkPric", 0) or 0),
                        "type": opt_type,
                        "open": float(r.get("OpnPric", 0) or 0),
                        "high": float(r.get("HghPric", 0) or 0),
                        "low": float(r.get("LwPric", 0) or 0),
                        "close": float(r.get("ClsPric", 0) or 0),
                        "prevClose": float(r.get("PrvsClsgPric", 0) or 0),
                        "spot": float(r.get("UndrlygPric", 0) or 0),
                        "oi": float(r.get("OpnIntrst", 0) or 0),
                        "oiChgRaw": float(r.get("ChngInOpnIntrst", 0) or 0),
                        "vol": float(r.get("TtlTradgVol", 0) or 0),
                    })
                except Exception:
                    continue
        return rows

    def load_user_csv(self, csv_content: str, is_today: bool = True):
        rows = self._parse_csv_text(csv_content)
        if is_today:
            self.today_rows = rows
            self.today_date = "Custom Upload"
            return len(rows)
        else:
            self.prev_map = {f"{r['symbol']}|{r['expiry']}|{r['type']}|{r['strike']}": r for r in rows}
            self.prev_date = "Custom Upload"
            return len(rows)

    def get_symbols(self):
        if not self.today_rows:
            return []
        return sorted(list({r["symbol"] for r in self.today_rows}))

    def get_indices_pulse(self):
        """
        Returns live indicator verdicts, option flow movement (Calls vs Puts volume share),
        ATM strikes, and top active options for major indices: NIFTY, BANKNIFTY, SENSEX, FINNIFTY, MIDCPNIFTY.
        """
        if not self.today_rows:
            try:
                self.fetch_latest_bhavcopy()
            except Exception:
                pass

        target_indices = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
        summary = {}

        up_quotes = {}
        if upstox_service.is_configured():
            try:
                up_quotes = upstox_service.get_all_indices_quotes()
            except Exception as e:
                print(f"[Upstox] get_indices_pulse quote error: {e}")

        # Prefer a live Upstox option chain over bhavcopy settlement data for
        # ALL 5 indices, not just SENSEX -- bhavcopy can be several days stale
        # (self.today_date may lag real market days), so even when a symbol
        # has bhavcopy candidates it can still be pointing at an
        # already-expired series. This mirrors the same live-first pattern
        # already used successfully in get_option_chain_for_symbol().
        #
        # Fetched concurrently (not one at a time in the loop below) --
        # five sequential Upstox option-chain fetches took 5-7+ seconds in
        # practice, which is long enough to noticeably delay unrelated
        # requests on a single-worker server. Fetching them in parallel
        # brings this down to roughly the slowest single fetch instead of
        # the sum of all five.
        live_chains = {}
        if upstox_service.is_configured():
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                future_to_sym = {ex.submit(upstox_service.fetch_option_chain, sym): sym for sym in target_indices}
                for future in concurrent.futures.as_completed(future_to_sym):
                    sym = future_to_sym[future]
                    try:
                        up_chain = future.result()
                        if up_chain and up_chain.get("rows"):
                            live_chains[sym] = up_chain
                    except Exception as e:
                        print(f"[Upstox] {sym} pulse chain error: {e}")

        for sym in target_indices:
            candidates = [r for r in self.today_rows if r["symbol"] == sym]
            if not candidates:
                candidates = [r for r in self.today_rows if r["symbol"].startswith(sym)]

            target_exp = ""
            chain_rows = []

            up_chain = live_chains.get(sym)
            if up_chain:
                chain_rows = up_chain["rows"]
                target_exp = up_chain.get("targetExpiry", "")

            if not chain_rows and candidates:
                expiries = sorted(list({r["expiry"] for r in candidates}))
                target_exp = expiries[0] if expiries else ""
                chain_rows = [r for r in candidates if r["expiry"] == target_exp] if target_exp else candidates

            # If still no rows and no live quote, skip
            if not chain_rows and sym not in up_quotes:
                continue

            # Determine spot price and daily variation
            spot = 0.0
            change = 0.0
            change_pct = 0.0
            day_open = 0.0
            day_high = 0.0
            day_low = 0.0
            data_status = "unavailable"

            if sym in up_quotes:
                q = up_quotes[sym]
                spot = q["last"]
                change = q["change"]
                change_pct = q["pct"]
                day_open = q.get("open", 0.0)
                day_high = q.get("high", 0.0)
                day_low = q.get("low", 0.0)
                data_status = "live"
            elif chain_rows and chain_rows[0].get("spot"):
                spot = chain_rows[0]["spot"]
                prev_spot = 0.0
                for prow in self.prev_map.values():
                    if prow.get("symbol") == sym and prow.get("spot", 0) > 0:
                        prev_spot = prow["spot"]
                        break
                change = round(spot - prev_spot, 2) if spot > 0 and prev_spot > 0 else 0.0
                change_pct = round((change / prev_spot) * 100.0, 2) if prev_spot > 0 else 0.0
                data_status = "live" if sym in live_chains else "delayed"

            # Reference close for %-change math (Business Rule 10: % change
            # must be mathematically consistent with price vs reference) --
            # derived from the same change value already shown, not a
            # separately-fetched figure that could silently drift from it.
            prev_close = round(spot - change, 2) if spot > 0 else 0.0

            step = 100.0 if sym in ("BANKNIFTY", "SENSEX") else (25.0 if sym == "MIDCPNIFTY" else 50.0)
            atm_strike = round(spot / step) * step if spot > 0 else 0.0

            call_vol = 0.0
            put_vol = 0.0
            call_oi = 0.0
            put_oi = 0.0
            calls = []
            puts = []
            matches_count = 0

            for r in chain_rows:
                v = r.get("vol", 0.0)
                o = r.get("oi", 0.0)
                l = r.get("low", 0.0)
                pc = r.get("prevClose", 0.0)

                if (5.80 <= l <= 8.45 and 38.0 <= pc <= 48.0) or (52 <= l <= 58 and 300 <= pc <= 349):
                    matches_count += 1

                if r["type"] == "CE":
                    call_vol += v
                    call_oi += o
                    calls.append(r)
                elif r["type"] == "PE":
                    put_vol += v
                    put_oi += o
                    puts.append(r)

            tot_vol = call_vol + put_vol
            call_vol_pct = round((call_vol / tot_vol) * 100.0, 1) if tot_vol > 0 else 50.0
            put_vol_pct = round((put_vol / tot_vol) * 100.0, 1) if tot_vol > 0 else 50.0

            if call_vol_pct >= 68.0:
                sentiment = "🚀 Heavy Call Surge (Strong Bullish Flow)"
                verdict = "🟢 STRONG BUY"
            elif call_vol_pct >= 55.0:
                sentiment = "🟢 Call Inflows (Bullish Bias)"
                verdict = "🟢 BUY"
            elif put_vol_pct >= 68.0:
                sentiment = "🔻 Heavy Put Surge (Strong Bearish Flow)"
                verdict = "🔴 STRONG SELL"
            elif put_vol_pct >= 55.0:
                sentiment = "🔴 Put Inflows (Bearish Bias)"
                verdict = "🔴 SELL"
            else:
                sentiment = "⚖️ Balanced Volume (Rangebound / Straddle Zone)"
                verdict = "WAIT / RANGE"

            # Per-strike OI distribution (for the OI-by-strike dashboard chart) --
            # these totals were already being accumulated above but never sent
            # to the frontend; add the breakdown now rather than a second pass.
            oi_by_strike = {}
            for r in calls:
                k = r["strike"]
                oi_by_strike.setdefault(k, {"strike": k, "callOi": 0.0, "putOi": 0.0})
                oi_by_strike[k]["callOi"] += r.get("oi", 0.0)
            for r in puts:
                k = r["strike"]
                oi_by_strike.setdefault(k, {"strike": k, "callOi": 0.0, "putOi": 0.0})
                oi_by_strike[k]["putOi"] += r.get("oi", 0.0)

            sorted_strikes = sorted(oi_by_strike.keys())
            oi_distribution = []
            if sorted_strikes:
                atm_idx = min(range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - atm_strike))
                start = max(0, min(atm_idx - 3, len(sorted_strikes) - 7))
                end = min(len(sorted_strikes), start + 7)
                oi_distribution = [oi_by_strike[s] for s in sorted_strikes[start:end]]

            calls_sorted = sorted(calls, key=lambda x: x.get("vol", 0.0), reverse=True)
            puts_sorted = sorted(puts, key=lambda x: x.get("vol", 0.0), reverse=True)

            top_call = {
                "strike": calls_sorted[0]["strike"],
                "close": calls_sorted[0]["close"],
                "vol": calls_sorted[0]["vol"],
                "chg": round(calls_sorted[0]["close"] - calls_sorted[0]["prevClose"], 2) if calls_sorted[0]["prevClose"] > 0 else 0.0,
                "chgPct": round(((calls_sorted[0]["close"] - calls_sorted[0]["prevClose"]) / calls_sorted[0]["prevClose"]) * 100.0, 1) if calls_sorted[0]["prevClose"] > 0 else 0.0
            } if calls_sorted else None

            top_put = {
                "strike": puts_sorted[0]["strike"],
                "close": puts_sorted[0]["close"],
                "vol": puts_sorted[0]["vol"],
                "chg": round(puts_sorted[0]["close"] - puts_sorted[0]["prevClose"], 2) if puts_sorted[0]["prevClose"] > 0 else 0.0,
                "chgPct": round(((puts_sorted[0]["close"] - puts_sorted[0]["prevClose"]) / puts_sorted[0]["prevClose"]) * 100.0, 1) if puts_sorted[0]["prevClose"] > 0 else 0.0
            } if puts_sorted else None

            # See compute_recommendation() at module level for the documented
            # rule itself (pytest-covered in tests/test_market_data.py).
            recommendation = compute_recommendation(sym, verdict, top_call, top_put, data_status)

            summary[sym] = {
                "symbol": sym,
                "spot": spot,
                "open": day_open,
                "high": day_high,
                "low": day_low,
                "prevClose": prev_close,
                "change": change,
                "changePct": change_pct,
                "atmStrike": atm_strike,
                "expiry": target_exp,
                "callVol": call_vol,
                "putVol": put_vol,
                "callVolPct": call_vol_pct,
                "putVolPct": put_vol_pct,
                "callOi": call_oi,
                "putOi": put_oi,
                "oiDistribution": oi_distribution,
                "dataStatus": data_status,
                "timestamp": now_ist_iso(),
                "sentiment": sentiment,
                "verdict": verdict,
                "topCall": top_call,
                "topPut": top_put,
                "recommendation": recommendation,
                "matchesCount": matches_count,
                "totalContracts": len(chain_rows)
            }

        return summary

    def fetch_live_option_chain(self, symbol="NIFTY", expiry=None):
        symbol = symbol.upper().strip()

        # 1. Try Upstox API directly if configured
        if upstox_service.is_configured():
            try:
                res = upstox_service.fetch_option_chain(symbol=symbol, expiry=expiry)
                if res and res.get("rows"):
                    return res
            except Exception as e:
                print(f"[Upstox] Live chain error for {symbol}, falling back to NSE: {e}")

        # 2. Existing NSE live-chain implementation kept as fallback
        cache_key = f"{symbol}|{expiry}"
        now = time.time()
        if cache_key in self.live_cache:
            ts, cached_res = self.live_cache[cache_key]
            if now - ts < 8:
                return cached_res

        s = self._get_session()
        self.init_nse_session()

        is_index = symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
        type_param = "Indices" if is_index else "Equity"

        r_info = s.get(f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}", headers={
            "Referer": "https://www.nseindia.com/option-chain",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }, timeout=10)

        if r_info.status_code != 200:
            raise RuntimeError(f"Could not load contract info for {symbol}: HTTP {r_info.status_code}")

        info = r_info.json()
        expiry_dates = info.get("expiryDates", [])
        if not expiry_dates:
            raise RuntimeError(f"No active expiry dates returned for {symbol}")

        target_expiry = expiry if expiry and expiry in expiry_dates else expiry_dates[0]

        url = f"https://www.nseindia.com/api/option-chain-v3?type={type_param}&symbol={symbol}&expiry={target_expiry}"
        r_chain = s.get(url, headers={
            "Referer": "https://www.nseindia.com/option-chain",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }, timeout=12)

        if r_chain.status_code != 200:
            raise RuntimeError(f"Could not fetch option chain: HTTP {r_chain.status_code}")

        chain_json = r_chain.json()
        records = chain_json.get("records", {})
        data_list = records.get("data", [])

        rows = []
        for item in data_list:
            strike = float(item.get("strikePrice", 0))
            for opt_type in ("CE", "PE"):
                sub = item.get(opt_type)
                if not sub:
                    continue
                ltp = float(sub.get("lastPrice", 0) or 0)
                change = float(sub.get("change", 0) or 0)
                prev_close = ltp - change if (ltp and change is not None) else 0.0
                oi = float(sub.get("openInterest", 0) or 0)
                oi_chg = float(sub.get("changeinOpenInterest", 0) or 0)
                vol = float(sub.get("totalTradedVolume", 0) or 0)
                spot = float(sub.get("underlyingValue", 0) or 0)
                exp_date = sub.get("expiryDate", target_expiry)
                open_p = float(sub.get("openPrice", 0) or 0)
                high_p = float(sub.get("highPrice", 0) or 0)
                low_p = float(sub.get("lowPrice", 0) or 0)

                rows.append({
                    "symbol": symbol.upper(),
                    "instr": "Index" if is_index else "Stock",
                    "expiry": exp_date,
                    "strike": strike,
                    "type": opt_type,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": ltp,
                    "prevClose": prev_close,
                    "spot": spot,
                    "oi": oi,
                    "oiChgRaw": oi_chg,
                    "vol": vol,
                })

        result = {
            "symbol": symbol.upper(),
            "targetExpiry": target_expiry,
            "expiryDates": expiry_dates,
            "rows": rows,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        self.live_cache[cache_key] = (now, result)
        return result

    def scan(self, options_list, filters):
        sym = filters.get("symSelect", "__ALL__")
        inst_idx = filters.get("instIdx", True)
        inst_stk = filters.get("instStk", True)
        opt_type = filters.get("optType", "BOTH")
        exp_select = filters.get("expSelect", "nearest")
        low_on = filters.get("lowOn", True)
        low_from = float(filters.get("lowFrom", 5.80))
        low_to = float(filters.get("lowTo", 8.45))
        prev_on = filters.get("prevOn", True)
        prev_from = float(filters.get("prevFrom", 38.0))
        prev_to = float(filters.get("prevTo", 48.0))
        tol = float(filters.get("tol", 0.0))
        min_oi = float(filters.get("minOi", 0.0))
        include_untraded = filters.get("includeUntraded", False)
        show_mode = filters.get("showMode", "matches")

        indicator = filters.get("indicator")

        # Expiry-safety guard: an expired contract must never be shown as
        # active (spec Business Rule, section 4). Bhavcopy can lag behind
        # the real wall-clock date (e.g. a stale cached file), so this
        # compares each contract's own expiry against TODAY'S REAL date,
        # not whatever date the settlement data happens to be dated for --
        # a contract that already expired stays excluded even from stale data.
        today_iso = datetime.now(IST).date().isoformat()

        nearest_map = {}
        if exp_select == "nearest":
            for r in options_list:
                if r["expiry"] < today_iso:
                    continue
                s = r["symbol"]
                if s not in nearest_map or r["expiry"] < nearest_map[s]:
                    nearest_map[s] = r["expiry"]

        processed = []
        has_instr_filter = (inst_idx and not inst_stk) or (inst_stk and not inst_idx)
        for r in options_list:
            if r["expiry"] < today_iso:
                continue
            if sym != "__ALL__" and r["symbol"] != sym:
                continue
            if has_instr_filter:
                if r["instr"] == "Index" and not inst_idx:
                    continue
                if r["instr"] == "Stock" and not inst_stk:
                    continue
            if opt_type != "BOTH" and r["type"] != opt_type:
                continue
            if exp_select == "nearest" and r["expiry"] != nearest_map.get(r["symbol"]):
                continue
            if not include_untraded and (r["vol"] <= 0 or r["close"] <= 0.05 or r["low"] <= 0.05):
                continue
            if r["oi"] < min_oi:
                continue

            key = f"{r['symbol']}|{r['expiry']}|{r['type']}|{r['strike']}"
            prev_r = self.prev_map.get(key)
            prev_close_used = prev_r["close"] if prev_r else r["prevClose"]
            prev_oi_used = prev_r["oi"] if prev_r else (r["oi"] - r["oiChgRaw"])
            
            oi_pct = None
            if prev_oi_used and prev_oi_used > 0:
                oi_pct = ((r["oi"] - prev_oi_used) / prev_oi_used) * 100.0

            has_activity = r["vol"] > 0 and r["close"] > 0.05 and r["low"] > 0.05
            is_ol = has_activity and (abs(r["open"] - r["low"]) <= tol) if r["open"] > 0 and r["low"] > 0 else False
            is_oh = has_activity and (abs(r["open"] - r["high"]) <= tol) if r["open"] > 0 and r["high"] > 0 else False

            low_match = not low_on or (low_from <= r["low"] <= low_to)
            prev_match = not prev_on or (prev_from <= prev_close_used <= prev_to)
            is_match = low_match and prev_match

            sig = ""
            if is_ol and is_oh:
                sig = "O=L=H"
            elif is_ol:
                sig = "O=L"
            elif is_oh:
                sig = "O=H"

            # Indicator 2: VIX-based targets and stop losses
            buy_low = round(r["low"], 2)
            buy_low_9 = round(r["low"] * 1.09, 2)
            buy_open = round(r["open"], 2)
            tgt_9 = round(r["low"] * 1.09, 2)
            tgt_18 = round(r["low"] * 1.18, 2)
            sl_5 = round(r["low"] * 0.95, 2)
            sl_9 = round(r["low"] * 0.91, 2)
            sl_18 = round(r["low"] * 0.82, 2)

            # Indicator 3: Formula ÷8 and ÷6.5 levels
            div_8 = round(prev_close_used / 8.0, 2) if prev_close_used > 0 else 0.0
            div_65 = round(prev_close_used / 6.5, 2) if prev_close_used > 0 else 0.0
            is_match3 = (300 <= prev_close_used <= 349) and ((52 <= r["low"] <= 58) or (div_8 <= r["low"] <= div_65))

            # Indicator-specific evaluation across all stocks & indices
            is_ind_match = False
            ind_tag = ""

            if indicator == "alphascan":
                exact_match = (52.0 <= r["low"] <= 58.0) and (300.0 <= prev_close_used <= 349.0)
                in_div = (div_8 > 0 and div_8 <= r["low"] <= div_65)
                in_low = (52.0 <= r["low"] <= 58.0)
                in_prem = (300.0 <= prev_close_used <= 349.0)
                is_ind_match = exact_match or in_div or in_low or in_prem
                ind_tag = "⭐ EXACT MATCH" if exact_match else ("÷8-÷6.5 Zone" if in_div else ("Low 52-58" if in_low else "Prem 300-349"))
            elif indicator == "trade_pred":
                is_buy = is_ol or (r["close"] > r["open"] and r["close"] >= prev_close_used)
                is_sell = is_oh or (r["close"] < r["open"] and r["close"] <= prev_close_used)
                is_ind_match = is_buy or is_sell
                ind_tag = "🟢 TP BUY" if is_buy else "🔴 TP SELL"
            elif indicator == "quantum":
                is_sup = (r["low"] <= r["open"] * 0.98 and r["close"] > r["open"])
                is_res = (r["high"] >= r["open"] * 1.02 and r["close"] < r["open"])
                in_frac = (div_8 > 0 and abs(r["low"] - div_8) <= div_8 * 0.15)
                is_ind_match = is_sup or is_res or in_frac
                ind_tag = "🟢 Support" if is_sup else ("🔴 Resistance" if is_res else "Quantum Level")
            elif indicator == "target_proj":
                in_band = (div_8 > 0 and div_8 <= r["low"] <= div_65)
                hit_t2 = r["high"] >= tgt_18
                hit_t1 = r["close"] >= tgt_9
                is_ind_match = in_band or hit_t2 or hit_t1
                ind_tag = "🎯 Target 2" if hit_t2 else ("🎯 Target 1" if hit_t1 else "Target Band")
            elif indicator == "ut":
                is_buy = is_ol or (r["close"] > r["open"] and r["close"] >= prev_close_used and r["low"] <= r["open"] * 0.98)
                is_sell = is_oh or (r["close"] < r["open"] and r["close"] <= prev_close_used)
                is_ind_match = is_buy or is_sell
                ind_tag = "🟢 UT BUY" if is_buy else "🔴 UT SELL"
            elif indicator == "supertrend":
                is_bull = (r["close"] >= r["open"] * 1.01 and r["close"] >= prev_close_used)
                is_bear = (r["close"] <= r["open"] * 0.99 and r["close"] <= prev_close_used)
                is_ind_match = is_bull or is_bear
                ind_tag = "📈 Bull Trend" if is_bull else "📉 Bear Trend"
            elif indicator == "orb15":
                is_ind_match = is_ol or (r["close"] >= r["high"] * 0.98 and r["vol"] > 0)
                ind_tag = "🚀 ORB Breakout"
            elif indicator == "vwap_rsi":
                is_ind_match = (r["low"] <= r["open"] * 0.97 and r["close"] > r["low"])
                ind_tag = "🌊 VWAP Reversion"
            elif indicator == "volume_oi":
                is_ind_match = (r["vol"] >= 5000) or (abs(oi_pct or 0) >= 10.0)
                ind_tag = "📊 Volume Spike"
            elif indicator == "candle_pattern":
                is_ind_match = is_ol or is_oh
                ind_tag = "🟢 Open=Low" if is_ol else "🔴 Open=High"
            elif indicator == "strat1":
                is_ind_match = is_match
                ind_tag = "1️⃣ S1 MATCH"
            elif indicator == "strat2":
                is_ind_match = is_ol or is_oh
                ind_tag = "2️⃣ S2 VIX Tgt"
            elif indicator == "strat3":
                is_ind_match = is_match3
                ind_tag = "3️⃣ S3 Formula"
            else:
                is_ind_match = is_match
                ind_tag = sig or "Active"

            item = dict(r)
            item.update({
                "prevCloseUsed": round(prev_close_used, 2),
                "oiPct": round(oi_pct, 2) if oi_pct is not None else None,
                "isOL": is_ol,
                "isOH": is_oh,
                "isMatch": is_match,
                "isMatch3": is_match3,
                "indicatorMatch": is_ind_match,
                "indicatorTag": ind_tag,
                "sig": sig,
                "buyLow": buy_low,
                "buyLow9": buy_low_9,
                "buyOpen": buy_open,
                "tgt9": tgt_9,
                "tgt18": tgt_18,
                "sl5": sl_5,
                "sl9": sl_9,
                "sl18": sl_18,
                "div8": div_8,
                "div65": div_65,
                "crossChecked": prev_r is not None
            })

            if indicator:
                if show_mode == "all" or is_ind_match:
                    processed.append(item)
            elif show_mode == "matches":
                if is_match:
                    processed.append(item)
            elif show_mode == "ol":
                if is_ol and (not low_on or low_match) and (not prev_on or prev_match):
                    processed.append(item)
            elif show_mode == "oh":
                if is_oh and (not low_on or low_match) and (not prev_on or prev_match):
                    processed.append(item)
            elif show_mode == "ol_oh":
                if (is_ol or is_oh) and (not low_on or low_match) and (not prev_on or prev_match):
                    processed.append(item)
            elif show_mode == "all" or show_mode == "any":
                if (not low_on or low_match) and (not prev_on or prev_match):
                    processed.append(item)

        stat_match = sum(1 for x in processed if x["isMatch"])
        stat_ol = sum(1 for x in processed if x["isOL"])
        stat_oh = sum(1 for x in processed if x["isOH"])
        stat_ind = sum(1 for x in processed if x.get("indicatorMatch"))

        return {
            "totalContracts": len(options_list),
            "resultCount": len(processed),
            "statMatch": stat_ind if indicator else stat_match,
            "statOL": stat_ol,
            "statOH": stat_oh,
            "statInd": stat_ind,
            "results": processed
        }

    def get_option_chain_for_symbol(self, symbol="NIFTY", expiry=None, indicator=None):
        """
        Returns a structured option chain table around ATM for the symbol,
        enriched with indicator calculations (UT Bot signals, AlphaScan formula,
        Trade Predictor levels, Quantum Matrix, Target Projections), Call & Put volume totals,
        and the 71% volume share indicator.
        """
        if not self.today_rows:
            try:
                self.fetch_latest_bhavcopy()
            except Exception:
                pass

        sym = symbol.upper().strip()
        if sym in ("NIFTY 50", "NIFTY50"):
            sym = "NIFTY"
        elif sym in ("BANK NIFTY", "NIFTY BANK"):
            sym = "BANKNIFTY"
        elif sym in ("NIFTY MIDCAP", "NIFTY MID SELECT", "MIDCAP"):
            sym = "MIDCPNIFTY"
        elif sym in ("NIFTY FIN SERVICE", "FIN NIFTY"):
            sym = "FINNIFTY"

        candidates = []
        chain_data_status = "unavailable"
        if upstox_service.is_configured():
            try:
                up_res = upstox_service.fetch_option_chain(symbol=sym, expiry=expiry)
                if up_res and up_res.get("rows"):
                    candidates = up_res["rows"]
                    chain_data_status = "live"
            except Exception as e:
                print(f"[Upstox] get_option_chain_for_symbol error for {sym}: {e}")

        if not candidates and self.today_rows:
            candidates = [r for r in self.today_rows if r["symbol"] == sym]
            if not candidates:
                # Fallback search
                candidates = [r for r in self.today_rows if r["symbol"].startswith(sym)]
            if candidates:
                chain_data_status = "delayed"

        if not candidates:
            available_symbols = sorted(list({r["symbol"] for r in self.today_rows}))[:50]
            return {
                "symbol": sym,
                "error": f"No data found for symbol {sym}",
                "availableSymbols": available_symbols
            }

        # Expiry-safety guard: never list or show an already-expired contract
        # as active (spec Business Rule, section 4) -- relevant mainly to the
        # bhavcopy fallback path, which can lag behind the real wall-clock
        # date; Upstox's own live chain wouldn't list a dead contract anyway.
        today_iso = datetime.now(IST).date().isoformat()
        candidates = [r for r in candidates if r["expiry"] >= today_iso]
        if not candidates:
            return {
                "symbol": sym,
                "error": f"No active (non-expired) contracts found for symbol {sym}",
                "availableSymbols": []
            }

        expiries = sorted(list({r["expiry"] for r in candidates}))
        target_expiry = expiry if expiry and expiry in expiries else expiries[0]

        chain_rows = [r for r in candidates if r["expiry"] == target_expiry]
        spot = chain_rows[0]["spot"] if chain_rows and chain_rows[0].get("spot") else 0.0

        # Group by strike
        strikes_map = {}
        call_vol_tot = 0.0
        put_vol_tot = 0.0
        active_ind = (indicator or "ut").lower().strip()

        matched_calls = 0
        matched_puts = 0
        buy_signals = 0
        sell_signals = 0

        def enrich_contract(r):
            nonlocal matched_calls, matched_puts, buy_signals, sell_signals
            if not r:
                return None
            low = r.get("low", 0.0)
            open_p = r.get("open", 0.0)
            high = r.get("high", 0.0)
            close_p = r.get("close", 0.0)
            prev_close = r.get("prevClose", 0.0)
            opt_type = r.get("type", "CE")
            
            tgt_9 = round(low * 1.09, 2) if low > 0 else 0.0
            tgt_18 = round(low * 1.18, 2) if low > 0 else 0.0
            sl_5 = round(low * 0.95, 2) if low > 0 else 0.0
            sl_9 = round(low * 0.91, 2) if low > 0 else 0.0
            sl_18 = round(low * 0.82, 2) if low > 0 else 0.0
            div_8 = round(close_p / 8.0, 2) if close_p > 0 else 0.0
            div_65 = round(close_p / 6.5, 2) if close_p > 0 else 0.0
            is_ol = abs(open_p - low) <= 0.05 if open_p > 0 and low > 0 else False
            is_oh = abs(open_p - high) <= 0.05 if open_p > 0 and high > 0 else False
            is_match = (5.80 <= low <= 8.45) and (38.0 <= prev_close <= 48.0)
            chg = round(close_p - prev_close, 2) if prev_close > 0 else 0.0
            chg_pct = round((chg / prev_close) * 100.0, 2) if prev_close > 0 else 0.0

            # UT Bot Alerts calculation
            atr = max(high - low, abs(high - prev_close), abs(low - prev_close)) if high > low else max(0.5, close_p * 0.04)
            ut_trailing_sl = round(max(0.05, close_p - 1.5 * atr), 2)
            if close_p > open_p and (chg >= 0 or is_ol):
                ut_signal = "BUY"
                ut_tag = f"🟢 BUY (SL: ₹{ut_trailing_sl})"
                buy_signals += 1
            elif close_p < open_p and chg <= 0:
                ut_signal = "SELL"
                ut_tag = "🔴 SELL"
                sell_signals += 1
            else:
                ut_signal = "NEUTRAL"
                ut_tag = "WAIT"

            # AlphaScan Matrix formula
            prem_ok = (300 <= prev_close <= 349) or (38 <= prev_close <= 48)
            low_ok = (52 <= low <= 58) or (5.80 <= low <= 8.45) or (div_8 > 0 and min(div_8, div_65) <= low <= max(div_8, div_65))
            alphascan_match = prem_ok and low_ok
            alphascan_tag = "💎 MATCH" if alphascan_match else ("Prem ✓" if prem_ok else ("Low ✓" if low_ok else ""))

            # Quantum Matrix
            quantum_frac = round(open_p * 0.88, 2) if open_p > 0 else 0.0
            quantum_sup = round(low * 0.96, 2) if low > 0 else 0.0
            quantum_res = round(high * 1.04, 2) if high > 0 else 0.0

            # Target Projections
            tp_status = "TARGET 1 MET" if high >= tgt_9 and tgt_9 > 0 else ("IN ZONE" if close_p >= low * 1.01 else "WAIT")

            # Determine Indicator Badge & Match based on active_ind
            ind_badge = ""
            ind_matched = False
            ind_note = ""

            if "ut" in active_ind:
                ind_badge = ut_tag
                ind_matched = (ut_signal == "BUY")
                ind_note = f"UT ATR SL: ₹{ut_trailing_sl}"
            elif "alpha" in active_ind:
                ind_badge = alphascan_tag if alphascan_tag else (f"÷8: ₹{div_8}" if div_8 > 0 else "")
                ind_matched = alphascan_match
                ind_note = f"÷8: ₹{div_8} | ÷6.5: ₹{div_65}"
            elif "pred" in active_ind:
                ind_badge = f"🎯 Tgt: ₹{tgt_9}" if close_p > open_p else "AVOID"
                ind_matched = (close_p > open_p)
                ind_note = f"Target: ₹{tgt_9} | SL: ₹{sl_5}"
            elif "target" in active_ind or "proj" in active_ind:
                ind_badge = f"T1: ₹{tgt_9} | SL: ₹{sl_5}"
                ind_matched = (high >= tgt_9 or close_p > low)
                ind_note = f"T1: ₹{tgt_9} (+9%) | T2: ₹{tgt_18} (+18%)"
            elif "quantum" in active_ind:
                ind_badge = f"Sup: ₹{quantum_sup}"
                ind_matched = (close_p >= quantum_frac)
                ind_note = f"Support: ₹{quantum_sup} | Resistance: ₹{quantum_res}"
            elif "strat1" in active_ind:
                ind_badge = "BUY ZONE ₹52-58" if (52 <= low <= 58 or 5.8 <= low <= 8.45) else ""
                ind_matched = (52 <= low <= 58 or 5.8 <= low <= 8.45)
                ind_note = f"Low: ₹{low} (Buy Range 52-58)"
            elif "strat2" in active_ind:
                ind_badge = "O=L BULLISH" if is_ol else ("O=H BEARISH" if is_oh else "")
                ind_matched = is_ol or is_oh
                ind_note = f"O: ₹{open_p} | L: ₹{low} | H: ₹{high}"
            elif "strat3" in active_ind:
                ind_badge = f"÷8: ₹{div_8} | ÷6.5: ₹{div_65}"
                ind_matched = (min(div_8, div_65) <= low <= max(div_8, div_65)) if div_8 > 0 else False
                ind_note = f"÷8.0 Level: ₹{div_8} | ÷6.5 Level: ₹{div_65}"
            else:
                ind_badge = ut_tag
                ind_matched = (ut_signal == "BUY")
                ind_note = f"Trailing SL: ₹{ut_trailing_sl}"

            if ind_matched:
                if opt_type == "CE":
                    matched_calls += 1
                else:
                    matched_puts += 1

            item = dict(r)
            item.update({
                "buyLow": low,
                "tgt9": tgt_9,
                "tgt18": tgt_18,
                "sl5": sl_5,
                "sl9": sl_9,
                "sl18": sl_18,
                "div8": div_8,
                "div65": div_65,
                "isOL": is_ol,
                "isOH": is_oh,
                "isMatch": is_match,
                "chg": chg,
                "chgPct": chg_pct,
                "utSignal": ut_signal,
                "utTag": ut_tag,
                "utTrailingSl": ut_trailing_sl,
                "alphascanMatch": alphascan_match,
                "alphascanTag": alphascan_tag,
                "quantumSup": quantum_sup,
                "quantumRes": quantum_res,
                "tpStatus": tp_status,
                "indBadge": ind_badge,
                "indMatched": ind_matched,
                "indNote": ind_note
            })
            return item

        for r in chain_rows:
            stk = r["strike"]
            if stk not in strikes_map:
                strikes_map[stk] = {"strike": stk, "CE": None, "PE": None}
            if r["type"] == "CE":
                strikes_map[stk]["CE"] = enrich_contract(r)
                call_vol_tot += r["vol"]
            elif r["type"] == "PE":
                strikes_map[stk]["PE"] = enrich_contract(r)
                put_vol_tot += r["vol"]

        # Calculate 71% volume indicator
        tot_vol = call_vol_tot + put_vol_tot
        call_pct = round((call_vol_tot / tot_vol) * 100.0, 1) if tot_vol > 0 else 0.0
        put_pct = round((put_vol_tot / tot_vol) * 100.0, 1) if tot_vol > 0 else 0.0
        
        vol_signal = "NEUTRAL"
        if call_pct >= 71.0:
            vol_signal = "BULLISH (BUY CALL) — Call Volume >= 71%"
        elif put_pct >= 71.0:
            vol_signal = "BEARISH (BUY PUT) — Put Volume >= 71%"

        # Sort strikes
        all_strikes = sorted(strikes_map.keys())
        # Find ATM strike
        atm_strike = min(all_strikes, key=lambda x: abs(x - spot)) if all_strikes and spot > 0 else (all_strikes[len(all_strikes)//2] if all_strikes else 0)

        # Slice around ATM (±12 strikes)
        if atm_strike in all_strikes:
            atm_idx = all_strikes.index(atm_strike)
            start_idx = max(0, atm_idx - 12)
            end_idx = min(len(all_strikes), atm_idx + 13)
            displayed_strikes = all_strikes[start_idx:end_idx]
        else:
            displayed_strikes = all_strikes[:25]

        strikes_data = [strikes_map[s] for s in displayed_strikes]

        return {
            "symbol": sym,
            "expiry": target_expiry,
            "allExpiries": expiries,
            "spot": spot,
            "atmStrike": atm_strike,
            "indicator": active_ind,
            "callVolumeTotal": call_vol_tot,
            "putVolumeTotal": put_vol_tot,
            "callVolumePct": call_pct,
            "putVolumePct": put_pct,
            "volumeSignal": vol_signal,
            "matchedCallsCount": matched_calls,
            "matchedPutsCount": matched_puts,
            "buySignalsCount": buy_signals,
            "sellSignalsCount": sell_signals,
            "strikes": strikes_data,
            "dataStatus": chain_data_status,
            "timestamp": now_ist_iso()
        }

    def clear_cached_data(self):
        """
        Deletes all stored CSV files from writable cache directory and clears in-memory state.
        Ensures storage on Vercel is clean and forces fresh live data to be fetched on Monday.
        """
        deleted_files = []
        errors = []

        # 1. Clean WRITABLE_CACHE_DIR (/tmp/data_cache on Vercel)
        if os.path.exists(WRITABLE_CACHE_DIR):
            for fname in os.listdir(WRITABLE_CACHE_DIR):
                fpath = os.path.join(WRITABLE_CACHE_DIR, fname)
                try:
                    if os.path.isfile(fpath) or os.path.islink(fpath):
                        os.remove(fpath)
                        deleted_files.append(fname)
                except Exception as e:
                    errors.append(f"Error deleting {fname}: {str(e)}")

        # 2. Also check /tmp for any stray BhavCopy or CSV files
        if os.environ.get("VERCEL") and os.path.exists("/tmp"):
            try:
                for fname in os.listdir("/tmp"):
                    if fname.endswith(".csv") or fname.endswith(".zip") or fname.startswith("BhavCopy"):
                        fpath = os.path.join("/tmp", fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                            deleted_files.append(f"/tmp/{fname}")
            except Exception as e:
                errors.append(f"Error checking /tmp: {str(e)}")

        # 3. Reset in-memory state so fresh live data is fetched on Monday
        self.today_rows = []
        self.prev_map = {}
        self.today_date = None
        self.prev_date = None
        self.last_fetch_time = None
        self.live_cache = {}
        self.session = None

        return {
            "success": True,
            "deletedFiles": deleted_files,
            "deletedCount": len(deleted_files),
            "errors": errors,
            "message": f"Successfully cleared {len(deleted_files)} stored CSV data files. Storage is clean."
        }

    def get_global_markets(self):
        """
        Returns live / latest quotes for Indian & Global Commodities (Gold MCX, Crude Oil MCX, Bitcoin BTC/INR),
        Indian Market Monthly Futures (GIFT Nifty, Nifty Near/Next Month, BankNifty), and US Market Futures (Dow, S&P 500, Nasdaq 100).
        """
        now = time.time()
        if hasattr(self, 'global_cache') and self.global_cache and (now - getattr(self, 'global_cache_time', 0) < 15):
            return self.global_cache

        import urllib.request
        syms = ['GC=F', 'CL=F', 'YM=F', 'ES=F', 'NQ=F', 'BTC-USD', 'INR=X']
        market_data = {}

        def _fetch_yahoo_symbol(s):
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1d&range=1d"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            with urllib.request.urlopen(req, timeout=3.0) as r:
                d = json.loads(r.read())
                meta = d['chart']['result'][0]['meta']
                price = meta.get('regularMarketPrice', 0.0)
                prev = meta.get('chartPreviousClose', price)
                chg = price - prev if price and prev else 0.0
                pct = (chg / prev) * 100.0 if prev else 0.0
                return {'price': price, 'prev': prev, 'chg': chg, 'pct': pct}

        # Fetched concurrently -- 7 sequential Yahoo calls (up to 3s timeout
        # each) could take up to 21s in the worst case, long enough to
        # noticeably delay unrelated requests on a single-worker server.
        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
            future_to_sym = {ex.submit(_fetch_yahoo_symbol, s): s for s in syms}
            for future in concurrent.futures.as_completed(future_to_sym):
                s = future_to_sym[future]
                try:
                    market_data[s] = future.result()
                except Exception:
                    pass

        # No hardcoded fallback numbers here (there used to be one per symbol,
        # e.g. "or 42850.0") -- a failed live fetch now means the field is
        # genuinely None and the response says so (Business Rule: never show
        # a fabricated value as if it were live data). See the "available"
        # flag built into each entry further down.
        inr_rate = market_data.get('INR=X', {}).get('price') or None
        inr_pct = market_data.get('INR=X', {}).get('pct')

        gold_usd = market_data.get('GC=F', {}).get('price') or None
        gold_pct = market_data.get('GC=F', {}).get('pct')
        gold_mcx = None
        if gold_usd and inr_rate:
            candidate = round((gold_usd / 31.1035) * 10.0 * inr_rate * 1.12)
            if candidate <= 100000:  # sanity bound; an absurd result is treated as unavailable, not swapped for a guess
                gold_mcx = candidate
        gold_mcx_chg = round(gold_mcx * (gold_pct / 100.0), 1) if (gold_mcx and gold_pct is not None) else None

        crude_usd = market_data.get('CL=F', {}).get('price') or None
        crude_pct = market_data.get('CL=F', {}).get('pct')
        crude_mcx = round(crude_usd * inr_rate) if (crude_usd and inr_rate) else None
        crude_mcx_chg = round(crude_mcx * (crude_pct / 100.0), 1) if (crude_mcx and crude_pct is not None) else None

        btc_usd = market_data.get('BTC-USD', {}).get('price') or None
        btc_pct = market_data.get('BTC-USD', {}).get('pct')
        btc_inr = round(btc_usd * inr_rate) if (btc_usd and inr_rate) else None
        btc_inr_chg = round(btc_inr * (btc_pct / 100.0), 1) if (btc_inr and btc_pct is not None) else None

        dow_fut = market_data.get('YM=F', {}).get('price') or None
        dow_pct = market_data.get('YM=F', {}).get('pct')
        dow_chg = market_data.get('YM=F', {}).get('chg')

        sp_fut = market_data.get('ES=F', {}).get('price') or None
        sp_pct = market_data.get('ES=F', {}).get('pct')
        sp_chg = market_data.get('ES=F', {}).get('chg')

        nasdaq_fut = market_data.get('NQ=F', {}).get('price') or None
        nasdaq_pct = market_data.get('NQ=F', {}).get('pct')
        nasdaq_chg = market_data.get('NQ=F', {}).get('chg')

        # Indian Spot reference -- prefer live Upstox quotes (same source already
        # proven live in get_market_status/get_indices_pulse) over the bhavcopy
        # settlement cache, which can be several days stale. This also gives a
        # real day change % to drive the futures chg/chgPct below instead of the
        # hardcoded constants that previously never changed.
        nifty_spot = None
        nifty_chg_pct = 0.0
        bank_spot = None
        bank_chg_pct = 0.0

        up_idx_quotes = {}
        try:
            if upstox_service.is_configured():
                up_idx_quotes = upstox_service.get_all_indices_quotes()
        except Exception as e:
            print(f"[Upstox] get_global_markets index quote error: {e}")

        if up_idx_quotes.get("NIFTY", {}).get("last"):
            nifty_spot = up_idx_quotes["NIFTY"]["last"]
            nifty_chg_pct = up_idx_quotes["NIFTY"]["pct"]
        elif self.today_rows:
            for r in self.today_rows:
                if r.get('symbol') == 'NIFTY' and r.get('spot', 0) > 0:
                    nifty_spot = r['spot']
                    break
            for prow in self.prev_map.values():
                if prow.get('symbol') == 'NIFTY' and prow.get('spot', 0) > 0 and nifty_spot > 0:
                    nifty_chg_pct = round(((nifty_spot - prow['spot']) / prow['spot']) * 100.0, 2)
                    break

        if up_idx_quotes.get("BANKNIFTY", {}).get("last"):
            bank_spot = up_idx_quotes["BANKNIFTY"]["last"]
            bank_chg_pct = up_idx_quotes["BANKNIFTY"]["pct"]
        elif self.today_rows:
            for r in self.today_rows:
                if r.get('symbol') == 'BANKNIFTY' and r.get('spot', 0) > 0:
                    bank_spot = r['spot']
                    break
            for prow in self.prev_map.values():
                if prow.get('symbol') == 'BANKNIFTY' and prow.get('spot', 0) > 0 and bank_spot > 0:
                    bank_chg_pct = round(((bank_spot - prow['spot']) / prow['spot']) * 100.0, 2)
                    break

        # NOTE: these four are an ESTIMATE (real spot + a fixed assumed
        # premium), not a real quote from NSE IX / the futures exchange --
        # labelled "(Est.)" in the response's `label`/`subLabel` so the UI
        # never implies this is a live futures-market tick. Real spot
        # unavailable => the estimate is None too, not computed from a guess.
        gift_nifty = round(nifty_spot + 68.0, 2) if nifty_spot else None
        nifty_fut_near = round(nifty_spot + 45.0, 2) if nifty_spot else None
        nifty_fut_next = round(nifty_spot + 128.0, 2) if nifty_spot else None
        bank_fut_near = round(bank_spot + 165.0, 2) if bank_spot else None

        gift_nifty_chg = round(gift_nifty * (nifty_chg_pct / 100.0), 2) if gift_nifty else None
        nifty_fut_near_chg = round(nifty_fut_near * (nifty_chg_pct / 100.0), 2) if nifty_fut_near else None
        nifty_fut_next_chg = round(nifty_fut_next * (nifty_chg_pct / 100.0), 2) if nifty_fut_next else None
        bank_fut_near_chg = round(bank_fut_near * (bank_chg_pct / 100.0), 2) if bank_fut_near else None

        def _entry(id_, name, label, price, fmt, chg, pct, sub_label=None, usd_price=None, usd_fmt=None, market=""):
            """Builds one ticker entry; `available=False` with no fabricated
            numbers when the underlying live fetch didn't come through."""
            available = price is not None and pct is not None
            return {
                "id": id_,
                "name": name,
                "label": label,
                "available": available,
                "price": price if available else None,
                "priceFormatted": (fmt.format(price) if available else "Unavailable"),
                "usdPrice": round(usd_price, 2) if (available and usd_price is not None) else None,
                "subLabel": (usd_fmt.format(usd_price) if (available and usd_price is not None and usd_fmt) else (sub_label or "")),
                "chg": chg if available else None,
                "chgPct": round(pct, 2) if available else None,
                "isPos": bool(available and pct >= 0),
                "market": market
            }

        data = {
            "success": True,
            "inrUsd": round(inr_rate, 2) if inr_rate else None,
            "inrChgPct": round(inr_pct, 2) if inr_pct is not None else None,
            "commodities": [
                _entry("gold", "GOLD (MCX)", "Gold 24K (₹/10g)", gold_mcx, "₹{:,.0f}", gold_mcx_chg, gold_pct,
                       usd_price=gold_usd, usd_fmt="${:,.1f}/oz", market="MCX / International"),
                _entry("crude", "CRUDE OIL", "Crude (₹/bbl)", crude_mcx, "₹{:,.0f}", crude_mcx_chg, crude_pct,
                       usd_price=crude_usd, usd_fmt="${:.2f}/bbl", market="MCX / WTI"),
                _entry("btc", "BITCOIN", "BTC / INR", btc_inr, "₹{:,.0f}", btc_inr_chg, btc_pct,
                       usd_price=btc_usd, usd_fmt="${:,.0f}", market="Crypto Spot"),
            ],
            "futures": [
                _entry("gift_nifty", "GIFT NIFTY", "NSE IX Futures (Est.)", gift_nifty, "{:,.2f}", gift_nifty_chg, nifty_chg_pct,
                       sub_label="Basis: +68.0 (estimated)", market="NSE IX"),
                _entry("nifty_fut_near", "NIFTY FUT (Near)", "Monthly Futures (Est.)", nifty_fut_near, "{:,.2f}", nifty_fut_near_chg, nifty_chg_pct,
                       sub_label="Premium: +45.0 (estimated)", market="NSE F&O"),
                _entry("nifty_fut_next", "NIFTY FUT (Next)", "Next Month Fut (Est.)", nifty_fut_next, "{:,.2f}", nifty_fut_next_chg, nifty_chg_pct,
                       sub_label="Roll Spread: +83.0 (estimated)", market="NSE F&O"),
                _entry("bank_fut_near", "BANK NIFTY FUT", "Monthly Futures (Est.)", bank_fut_near, "{:,.2f}", bank_fut_near_chg, bank_chg_pct,
                       sub_label="Premium: +165.0 (estimated)", market="NSE F&O"),
                _entry("dow_fut", "DOW FUTURES", "US 30 (YM)", dow_fut, "{:,.0f}", dow_chg, dow_pct,
                       sub_label="CBOT Mini", market="US CBOT"),
                _entry("sp_fut", "S&P 500 FUT", "E-mini S&P (ES)", sp_fut, "{:,.2f}", sp_chg, sp_pct,
                       sub_label="CME Futures", market="US CME"),
                _entry("nasdaq_fut", "NASDAQ FUT", "E-mini NQ 100", nasdaq_fut, "{:,.2f}", nasdaq_chg, nasdaq_pct,
                       sub_label="CME Futures", market="US CME"),
            ],
            # Spec section 1/7: explicitly unavailable rather than silently
            # absent -- no US equity or broader-crypto data provider is
            # configured for this app (only Upstox for Indian F&O/indices and
            # a single BTC/INR quote above).
            "usStocks": {"available": False, "reason": "No live US equity data source is configured for this app"},
            "otherCrypto": {"available": False, "reason": "Only BTC/INR is available via the current data source; no provider for other crypto assets is configured"},
        }
        self.global_cache = data
        self.global_cache_time = now
        return data

nse_service = NSEService()

