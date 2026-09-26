import io
import os
import csv
import json
import time
import zipfile
from datetime import datetime
from curl_cffi import requests

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

    def get_market_status(self):
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
                    "todayDateLoaded": self.today_date,
                    "prevDateLoaded": self.prev_date,
                    "totalLoaded": len(self.today_rows),
                }
        except Exception as e:
            print(f"[NSE] marketStatus error: {e}")
        return {
            "marketStatus": "Close",
            "tradeDate": self.today_date or "",
            "todayDateLoaded": self.today_date,
            "prevDateLoaded": self.prev_date,
            "totalLoaded": len(self.today_rows),
        }

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

    def fetch_live_option_chain(self, symbol="NIFTY", expiry=None):
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

        nearest_map = {}
        if exp_select == "nearest":
            for r in options_list:
                s = r["symbol"]
                if s not in nearest_map or r["expiry"] < nearest_map[s]:
                    nearest_map[s] = r["expiry"]

        processed = []
        for r in options_list:
            if sym != "__ALL__" and r["symbol"] != sym:
                continue
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

            item = dict(r)
            item.update({
                "prevCloseUsed": round(prev_close_used, 2),
                "oiPct": round(oi_pct, 2) if oi_pct is not None else None,
                "isOL": is_ol,
                "isOH": is_oh,
                "isMatch": is_match,
                "isMatch3": is_match3,
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

            if show_mode == "all":
                processed.append(item)
            elif show_mode == "matches" and is_match:
                processed.append(item)
            elif show_mode == "ol" and is_ol:
                processed.append(item)
            elif show_mode == "oh" and is_oh:
                processed.append(item)
            elif show_mode == "any" and (is_match or is_ol or is_oh):
                processed.append(item)

        stat_match = sum(1 for x in processed if x["isMatch"])
        stat_ol = sum(1 for x in processed if x["isOL"])
        stat_oh = sum(1 for x in processed if x["isOH"])

        return {
            "totalContracts": len(options_list),
            "resultCount": len(processed),
            "statMatch": stat_match,
            "statOL": stat_ol,
            "statOH": stat_oh,
            "results": processed
        }

    def get_option_chain_for_symbol(self, symbol="NIFTY", expiry=None):
        """
        Returns a structured option chain table around ATM for the symbol,
        including Call & Put volume totals and the 71% volume share indicator.
        """
        sym = symbol.upper()
        candidates = [r for r in self.today_rows if r["symbol"] == sym]
        if not candidates:
            return {"symbol": sym, "error": f"No data found for symbol {sym}"}

        expiries = sorted(list({r["expiry"] for r in candidates}))
        target_expiry = expiry if expiry and expiry in expiries else expiries[0]

        chain_rows = [r for r in candidates if r["expiry"] == target_expiry]
        spot = chain_rows[0]["spot"] if chain_rows and chain_rows[0].get("spot") else 0.0

        # Group by strike
        strikes_map = {}
        call_vol_tot = 0.0
        put_vol_tot = 0.0

        for r in chain_rows:
            stk = r["strike"]
            if stk not in strikes_map:
                strikes_map[stk] = {"strike": stk, "CE": None, "PE": None}
            if r["type"] == "CE":
                strikes_map[stk]["CE"] = r
                call_vol_tot += r["vol"]
            elif r["type"] == "PE":
                strikes_map[stk]["PE"] = r
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
            "callVolumeTotal": call_vol_tot,
            "putVolumeTotal": put_vol_tot,
            "callVolumePct": call_pct,
            "putVolumePct": put_pct,
            "volumeSignal": vol_signal,
            "strikes": strikes_data
        }

nse_service = NSEService()
