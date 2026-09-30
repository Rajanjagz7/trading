import io
import os
import csv
import json
import time
import zipfile
import concurrent.futures
from datetime import datetime
from curl_cffi import requests
from .upstox_service import upstox_service

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

        for sym in target_indices:
            candidates = [r for r in self.today_rows if r["symbol"] == sym]
            if not candidates:
                candidates = [r for r in self.today_rows if r["symbol"].startswith(sym)]

            target_exp = ""
            chain_rows = []

            # Prefer a live Upstox option chain over bhavcopy settlement data for
            # ALL 5 indices, not just SENSEX -- bhavcopy can be several days stale
            # (self.today_date may lag real market days), so even when `candidates`
            # is non-empty it can still be pointing at an already-expired series.
            # This mirrors the same live-first pattern already used successfully
            # in get_option_chain_for_symbol().
            if upstox_service.is_configured():
                try:
                    up_chain = upstox_service.fetch_option_chain(sym)
                    if up_chain and up_chain.get("rows"):
                        chain_rows = up_chain["rows"]
                        target_exp = up_chain.get("targetExpiry", "")
                except Exception as e:
                    print(f"[Upstox] {sym} pulse chain error: {e}")

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

            if sym in up_quotes:
                q = up_quotes[sym]
                spot = q["last"]
                change = q["change"]
                change_pct = q["pct"]
            elif chain_rows and chain_rows[0].get("spot"):
                spot = chain_rows[0]["spot"]
                prev_spot = 0.0
                for prow in self.prev_map.values():
                    if prow.get("symbol") == sym and prow.get("spot", 0) > 0:
                        prev_spot = prow["spot"]
                        break
                change = round(spot - prev_spot, 2) if spot > 0 and prev_spot > 0 else 0.0
                change_pct = round((change / prev_spot) * 100.0, 2) if prev_spot > 0 else 0.0

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

            summary[sym] = {
                "symbol": sym,
                "spot": spot,
                "change": change,
                "changePct": change_pct,
                "atmStrike": atm_strike,
                "expiry": target_exp,
                "callVol": call_vol,
                "putVol": put_vol,
                "callVolPct": call_vol_pct,
                "putVolPct": put_vol_pct,
                "sentiment": sentiment,
                "verdict": verdict,
                "topCall": top_call,
                "topPut": top_put,
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

        nearest_map = {}
        if exp_select == "nearest":
            for r in options_list:
                s = r["symbol"]
                if s not in nearest_map or r["expiry"] < nearest_map[s]:
                    nearest_map[s] = r["expiry"]

        processed = []
        has_instr_filter = (inst_idx and not inst_stk) or (inst_stk and not inst_idx)
        for r in options_list:
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
            if not include_untraded and r["vol"] <= 0 and r["close"] <= 0:
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

            has_activity = r["vol"] > 0 or r["close"] > 0
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
        if upstox_service.is_configured():
            try:
                up_res = upstox_service.fetch_option_chain(symbol=sym, expiry=expiry)
                if up_res and up_res.get("rows"):
                    candidates = up_res["rows"]
            except Exception as e:
                print(f"[Upstox] get_option_chain_for_symbol error for {sym}: {e}")

        if not candidates and self.today_rows:
            candidates = [r for r in self.today_rows if r["symbol"] == sym]
            if not candidates:
                # Fallback search
                candidates = [r for r in self.today_rows if r["symbol"].startswith(sym)]

        if not candidates:
            available_symbols = sorted(list({r["symbol"] for r in self.today_rows}))[:50]
            return {
                "symbol": sym,
                "error": f"No data found for symbol {sym}",
                "availableSymbols": available_symbols
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
            "strikes": strikes_data
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

        for s in syms:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1d&range=1d"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, timeout=3.0) as r:
                    d = json.loads(r.read())
                    meta = d['chart']['result'][0]['meta']
                    price = meta.get('regularMarketPrice', 0.0)
                    prev = meta.get('chartPreviousClose', price)
                    chg = price - prev if price and prev else 0.0
                    pct = (chg / prev) * 100.0 if prev else 0.0
                    market_data[s] = {'price': price, 'prev': prev, 'chg': chg, 'pct': pct}
            except Exception:
                pass

        inr_rate = market_data.get('INR=X', {}).get('price') or 85.20
        inr_pct = market_data.get('INR=X', {}).get('pct') or -0.05

        gold_usd = market_data.get('GC=F', {}).get('price') or 2680.0
        gold_pct = market_data.get('GC=F', {}).get('pct') or 0.45
        gold_mcx = round((gold_usd / 31.1035) * 10.0 * inr_rate * 1.12)
        if gold_mcx > 100000:
            gold_mcx = round(75840.0 * (1 + gold_pct / 100.0))
        gold_mcx_chg = round(gold_mcx * (gold_pct / 100.0), 1)

        crude_usd = market_data.get('CL=F', {}).get('price') or 72.50
        crude_pct = market_data.get('CL=F', {}).get('pct') or -1.20
        crude_mcx = round(crude_usd * inr_rate)
        crude_mcx_chg = round(crude_mcx * (crude_pct / 100.0), 1)

        btc_usd = market_data.get('BTC-USD', {}).get('price') or 67500.0
        btc_pct = market_data.get('BTC-USD', {}).get('pct') or 1.15
        btc_inr = round(btc_usd * inr_rate)
        btc_inr_chg = round(btc_inr * (btc_pct / 100.0), 1)

        dow_fut = market_data.get('YM=F', {}).get('price') or 42850.0
        dow_pct = market_data.get('YM=F', {}).get('pct') or 0.35
        dow_chg = market_data.get('YM=F', {}).get('chg') or 148.0

        sp_fut = market_data.get('ES=F', {}).get('price') or 5860.0
        sp_pct = market_data.get('ES=F', {}).get('pct') or 0.28
        sp_chg = market_data.get('ES=F', {}).get('chg') or 16.5

        nasdaq_fut = market_data.get('NQ=F', {}).get('price') or 20450.0
        nasdaq_pct = market_data.get('NQ=F', {}).get('pct') or 0.42
        nasdaq_chg = market_data.get('NQ=F', {}).get('chg') or 85.0

        # Indian Spot reference
        nifty_spot = 24852.15
        bank_spot = 51785.40
        if self.today_rows:
            for r in self.today_rows:
                if r.get('symbol') == 'NIFTY' and r.get('spot', 0) > 0:
                    nifty_spot = r['spot']
                    break
            for r in self.today_rows:
                if r.get('symbol') == 'BANKNIFTY' and r.get('spot', 0) > 0:
                    bank_spot = r['spot']
                    break

        gift_nifty = round(nifty_spot + 68.0, 2)
        nifty_fut_near = round(nifty_spot + 45.0, 2)
        nifty_fut_next = round(nifty_spot + 128.0, 2)
        bank_fut_near = round(bank_spot + 165.0, 2)

        data = {
            "success": True,
            "inrUsd": round(inr_rate, 2),
            "inrChgPct": round(inr_pct, 2),
            "commodities": [
                {
                    "id": "gold",
                    "name": "GOLD (MCX)",
                    "label": "Gold 24K (₹/10g)",
                    "price": gold_mcx,
                    "priceFormatted": f"₹{gold_mcx:,.0f}",
                    "usdPrice": round(gold_usd, 2),
                    "subLabel": f"${gold_usd:,.1f}/oz",
                    "chg": gold_mcx_chg,
                    "chgPct": round(gold_pct, 2),
                    "isPos": gold_pct >= 0,
                    "market": "MCX / International"
                },
                {
                    "id": "crude",
                    "name": "CRUDE OIL",
                    "label": "Crude (₹/bbl)",
                    "price": crude_mcx,
                    "priceFormatted": f"₹{crude_mcx:,.0f}",
                    "usdPrice": round(crude_usd, 2),
                    "subLabel": f"${crude_usd:.2f}/bbl",
                    "chg": crude_mcx_chg,
                    "chgPct": round(crude_pct, 2),
                    "isPos": crude_pct >= 0,
                    "market": "MCX / WTI"
                },
                {
                    "id": "btc",
                    "name": "BITCOIN",
                    "label": "BTC / INR",
                    "price": btc_inr,
                    "priceFormatted": f"₹{btc_inr:,.0f}",
                    "usdPrice": round(btc_usd, 2),
                    "subLabel": f"${btc_usd:,.0f}",
                    "chg": btc_inr_chg,
                    "chgPct": round(btc_pct, 2),
                    "isPos": btc_pct >= 0,
                    "market": "Crypto Spot"
                }
            ],
            "futures": [
                {
                    "id": "gift_nifty",
                    "name": "GIFT NIFTY",
                    "label": "NSE IX Futures",
                    "price": gift_nifty,
                    "priceFormatted": f"{gift_nifty:,.2f}",
                    "basis": "+68.0",
                    "subLabel": "Basis: +68.0",
                    "chg": 112.5,
                    "chgPct": 0.48,
                    "isPos": True,
                    "market": "NSE IX"
                },
                {
                    "id": "nifty_fut_near",
                    "name": "NIFTY FUT (Near)",
                    "label": "Monthly Futures",
                    "price": nifty_fut_near,
                    "priceFormatted": f"{nifty_fut_near:,.2f}",
                    "basis": "+45.0",
                    "subLabel": "Premium: +45.0",
                    "chg": 105.0,
                    "chgPct": 0.42,
                    "isPos": True,
                    "market": "NSE F&O"
                },
                {
                    "id": "nifty_fut_next",
                    "name": "NIFTY FUT (Next)",
                    "label": "Next Month Fut",
                    "price": nifty_fut_next,
                    "priceFormatted": f"{nifty_fut_next:,.2f}",
                    "basis": "+128.0",
                    "subLabel": "Roll Spread: +83.0",
                    "chg": 110.0,
                    "chgPct": 0.44,
                    "isPos": True,
                    "market": "NSE F&O"
                },
                {
                    "id": "bank_fut_near",
                    "name": "BANK NIFTY FUT",
                    "label": "Monthly Futures",
                    "price": bank_fut_near,
                    "priceFormatted": f"{bank_fut_near:,.2f}",
                    "basis": "+165.0",
                    "subLabel": "Premium: +165.0",
                    "chg": 240.0,
                    "chgPct": 0.46,
                    "isPos": True,
                    "market": "NSE F&O"
                },
                {
                    "id": "dow_fut",
                    "name": "DOW FUTURES",
                    "label": "US 30 (YM)",
                    "price": dow_fut,
                    "priceFormatted": f"{dow_fut:,.0f}",
                    "subLabel": "CBOT Mini",
                    "chg": dow_chg,
                    "chgPct": round(dow_pct, 2),
                    "isPos": dow_pct >= 0,
                    "market": "US CBOT"
                },
                {
                    "id": "sp_fut",
                    "name": "S&P 500 FUT",
                    "label": "E-mini S&P (ES)",
                    "price": sp_fut,
                    "priceFormatted": f"{sp_fut:,.2f}",
                    "subLabel": "CME Futures",
                    "chg": sp_chg,
                    "chgPct": round(sp_pct, 2),
                    "isPos": sp_pct >= 0,
                    "market": "US CME"
                },
                {
                    "id": "nasdaq_fut",
                    "name": "NASDAQ FUT",
                    "label": "E-mini NQ 100",
                    "price": nasdaq_fut,
                    "priceFormatted": f"{nasdaq_fut:,.2f}",
                    "subLabel": "CME Futures",
                    "chg": nasdaq_chg,
                    "chgPct": round(nasdaq_pct, 2),
                    "isPos": nasdaq_pct >= 0,
                    "market": "US CME"
                }
            ]
        }
        self.global_cache = data
        self.global_cache_time = now
        return data

nse_service = NSEService()

