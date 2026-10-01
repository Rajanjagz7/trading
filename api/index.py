import os
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

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

from .nse_service import nse_service, now_ist_iso
from .upstox_service import upstox_service

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        print("[STARTUP] Auto-fetching latest Bhavcopy from NSE...")
        res = nse_service.fetch_latest_bhavcopy(force_refresh=False)
        print(f"[STARTUP] Successfully loaded {res['totalContracts']} contracts for {res['todayDate']}.")
    except Exception as e:
        print(f"[STARTUP] Notice: Startup fetch: {e}")
    yield

app = FastAPI(title="NSE Bhavcopy & Live Option Scanner API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScanRequest(BaseModel):
    symSelect: Optional[str] = "__ALL__"
    instIdx: Optional[bool] = True
    instStk: Optional[bool] = True
    optType: Optional[str] = "BOTH"
    expSelect: Optional[str] = "nearest"
    lowOn: Optional[bool] = True
    lowFrom: Optional[float] = 5.80
    lowTo: Optional[float] = 8.45
    prevOn: Optional[bool] = True
    prevFrom: Optional[float] = 38.0
    prevTo: Optional[float] = 48.0
    tol: Optional[float] = 0.0
    minOi: Optional[float] = 0.0
    includeUntraded: Optional[bool] = False
    showMode: Optional[str] = "matches"
    indicator: Optional[str] = None

@app.get("/api/status")
def get_status():
    status = nse_service.get_market_status()
    if isinstance(status, dict):
        status["version"] = "2.2.0"
        status["buildTime"] = "2026-09-27T07:30:00Z"
    return status

@app.get("/api/upstox/status")
def get_upstox_status():
    """Authenticated Upstox REST integration status."""
    return upstox_service.health()

@app.get("/api/news")
def get_market_news():
    """
    Live Market News (spec section 6). No news provider is configured for
    this app (no API key/account) -- this honestly reports that instead of
    fabricating headlines or timestamps. Swap in a real provider (e.g.
    NewsAPI) here when one is configured; the frontend already renders
    whatever `headlines` contains and falls back to "unavailable" when empty.
    """
    return {
        "available": False,
        "reason": "No live news provider is configured for this app yet",
        "headlines": [],
        "timestamp": now_ist_iso(),
    }

@app.get("/api/quotes/fast")
def get_fast_quotes():
    """
    Lightweight quote-only poll target for the connection-status indicator
    and ticker values -- just the already-3s-cached Upstox quote dict, no
    option-chain/OI/sentiment recomputation, so this is cheap enough to
    poll on a short interval without hammering Upstox or this server.
    """
    if not upstox_service.is_configured():
        return {"configured": False, "quotes": {}, "dataStatus": "unavailable"}
    try:
        quotes = upstox_service.get_all_indices_quotes()
        return {"configured": True, "quotes": quotes, "dataStatus": "live" if quotes else "unavailable"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fast quote error: {str(e)}")

@app.get("/api/candles")
def get_candles(symbol: str = "NIFTY", instrumentKey: Optional[str] = None, timeframe: str = "5minute"):
    """
    Real OHLCV candle series from Upstox -- no synthetic/estimated points.
    Pass instrumentKey directly for a specific option contract (the scanner/
    chain already returns it per-row); omit it to chart the underlying
    index/stock itself, resolved from `symbol`.
    """
    if not upstox_service.is_configured():
        raise HTTPException(status_code=503, detail="Upstox is not configured -- no real historical data source available")
    try:
        key = instrumentKey
        if not key:
            key, _ = upstox_service._resolve_underlying(symbol.upper().strip())
        candles = upstox_service.get_candles(key, timeframe)
        return {
            "symbol": symbol,
            "instrumentKey": key,
            "timeframe": timeframe,
            "candles": candles,
            "count": len(candles),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Candle fetch error: {str(e)}")

@app.get("/api/indices/pulse")
def get_indices_pulse():
    """
    Returns live indicator verdicts, Call/Put volume share, ATM strikes,
    and top active options for major indices: NIFTY, BANKNIFTY, FINNIFTY, SENSEX, MIDCPNIFTY.
    """
    try:
        return nse_service.get_indices_pulse()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/market/global")
def get_global_markets():
    """
    Returns live prices for Indian & Global Commodities (Gold MCX, Crude Oil MCX, Bitcoin BTC/INR),
    Indian Market Monthly Futures (GIFT Nifty, Nifty Near/Next Month, BankNifty), and US Market Futures (Dow, S&P 500, Nasdaq 100).
    """
    try:
        return nse_service.get_global_markets()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cleanup")
@app.post("/api/cleanup")
def cleanup_stored_csvs():
    """
    Weekly Cron Job running every Sunday at 12:00 AM (midnight).
    Deletes all stored CSV bhavcopy files from /tmp and clears memory
    so storage remains clean and Monday starts with 100% fresh live market data.
    """
    res = nse_service.clear_cached_data()
    res["executedAt"] = datetime.now().isoformat()
    return res

@app.get("/api/cron")
@app.post("/api/cron")
def scheduled_cron(action: Optional[str] = None):
    """
    Automated Cron Job:
    - If action=='cleanup' or executed on Sunday, executes weekly CSV cleanup.
    - Otherwise (Mon-Fri 9:16 AM AND again 5 minutes later at 9:21 AM IST — two
      separate Vercel cron triggers, as a redundant retry in case the 9:16 AM
      run hits a transient NSE fetch failure), refreshes Bhavcopy data and
      primes cache right after market opening.
    """
    now = datetime.now()
    if action == "cleanup" or now.weekday() == 6:  # 6 is Sunday
        cleanup_res = nse_service.clear_cached_data()
        return {
            "success": True,
            "action": "weekly_cleanup",
            "message": "Sunday 12:00 AM weekly CSV cleanup executed. All stored data cleared for Monday.",
            "executedAt": now.isoformat(),
            **cleanup_res
        }

    try:
        res = nse_service.fetch_latest_bhavcopy(force_refresh=True)
        return {
            "success": True,
            "action": "daily_sync",
            "message": "9:16 AM IST cron sync executed successfully",
            "executedAt": now.isoformat(),
            "todayDate": res.get("todayDate"),
            "prevDate": res.get("prevDate"),
            "totalContracts": res.get("totalContracts", 0)
        }
    except Exception as e:
        return {
            "success": False,
            "action": "daily_sync",
            "message": f"9:16 AM sync notice: {str(e)}",
            "executedAt": now.isoformat(),
            "cachedContracts": len(nse_service.today_rows)
        }

@app.post("/api/fetch-latest")
def fetch_latest(force: bool = False):
    try:
        res = nse_service.fetch_latest_bhavcopy(force_refresh=force)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/symbols")
def get_symbols():
    return {"symbols": nse_service.get_symbols()}

@app.get("/api/chain")
def get_chain(symbol: str = "NIFTY", expiry: Optional[str] = None, indicator: Optional[str] = None):
    try:
        res = nse_service.get_option_chain_for_symbol(symbol=symbol, expiry=expiry, indicator=indicator)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/scan")
def scan_bhavcopy(req: ScanRequest):
    if not nse_service.today_rows:
        try:
            nse_service.fetch_latest_bhavcopy()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"No Bhavcopy loaded: {str(e)}")

    filters = req.model_dump()
    result = nse_service.scan(nse_service.today_rows, filters)
    result["todayDate"] = nse_service.today_date
    result["prevDate"] = nse_service.prev_date
    return result

@app.get("/api/live/scan")
def live_scan(
    symbol: str = "NIFTY",
    expiry: Optional[str] = None,
    optType: str = "BOTH",
    lowOn: bool = True,
    lowFrom: float = 5.80,
    lowTo: float = 8.45,
    prevOn: bool = True,
    prevFrom: float = 38.0,
    prevTo: float = 48.0,
    tol: float = 0.0,
    minOi: float = 0.0,
    includeUntraded: bool = False,
    showMode: str = "matches",
    indicator: Optional[str] = None
):
    try:
        live_res = nse_service.fetch_live_option_chain(symbol=symbol, expiry=expiry)
        filters = {
            "symSelect": symbol.upper(),
            "instIdx": True,
            "instStk": True,
            "optType": optType,
            "expSelect": "all",
            "lowOn": lowOn,
            "lowFrom": lowFrom,
            "lowTo": lowTo,
            "prevOn": prevOn,
            "prevFrom": prevFrom,
            "prevTo": prevTo,
            "tol": tol,
            "minOi": minOi,
            "includeUntraded": includeUntraded,
            "showMode": showMode,
            "indicator": indicator
        }
        scan_output = nse_service.scan(live_res["rows"], filters)
        scan_output["symbol"] = live_res["symbol"]
        scan_output["targetExpiry"] = live_res["targetExpiry"]
        scan_output["expiryDates"] = live_res["expiryDates"]
        scan_output["liveTimestamp"] = live_res["timestamp"]
        scan_output["provider"] = live_res.get("provider", "NSE")
        scan_output["dataMode"] = live_res.get("mode", "NSE")
        return scan_output
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Live scan error: {str(e)}")

@app.get("/api/live/chain")
def get_live_chain(symbol: str = "NIFTY", expiry: Optional[str] = None):
    try:
        live_res = nse_service.fetch_live_option_chain(symbol=symbol, expiry=expiry)
        raw_rows = live_res.get("rows", [])
        if not raw_rows:
            return live_res

        strikes_map = {}
        spot = 0.0
        for r in raw_rows:
            if r.get("spot"):
                spot = float(r["spot"])
            k = float(r["strike"])
            if k not in strikes_map:
                strikes_map[k] = {"strike": k, "ce": None, "pe": None}
            opt_type = r["type"].upper()
            if opt_type == "CE":
                strikes_map[k]["ce"] = r
            elif opt_type == "PE":
                strikes_map[k]["pe"] = r

        sorted_strikes = sorted(strikes_map.keys())
        if spot <= 0 and sorted_strikes:
            spot = sorted_strikes[len(sorted_strikes) // 2]

        atm_strike = min(sorted_strikes, key=lambda x: abs(x - spot)) if sorted_strikes else 0
        atm_idx = sorted_strikes.index(atm_strike) if atm_strike in sorted_strikes else 0
        min_i = max(0, atm_idx - 18)
        max_i = min(len(sorted_strikes), atm_idx + 19)
        window_strikes = sorted_strikes[min_i:max_i]

        chain_table = [strikes_map[k] for k in window_strikes]

        total_ce_vol = sum((r["ce"]["vol"] if r["ce"] else 0) for r in chain_table)
        total_pe_vol = sum((r["pe"]["vol"] if r["pe"] else 0) for r in chain_table)
        tot_vol = total_ce_vol + total_pe_vol
        call_pct = round((total_ce_vol / tot_vol * 100), 1) if tot_vol > 0 else 50.0
        put_pct = round(100.0 - call_pct, 1)

        return {
            "symbol": live_res.get("symbol", symbol.upper()),
            "spot": spot,
            "atmStrike": atm_strike,
            "expiry": live_res.get("targetExpiry"),
            "allExpiries": live_res.get("expiryDates", []),
            "rows": chain_table,
            "totalContracts": len(raw_rows),
            "provider": live_res.get("provider", "Upstox"),
            "mode": live_res.get("mode", "REST"),
            "timestamp": live_res.get("timestamp"),
            "volumeShare": {
                "callVol": total_ce_vol,
                "putVol": total_pe_vol,
                "callPct": call_pct,
                "putPct": put_pct,
                "sentiment": "BULLISH" if call_pct >= 60 else ("BEARISH" if call_pct <= 40 else "NEUTRAL")
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Live chain error: {str(e)}")

@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...), isToday: bool = Form(True)):
    try:
        content = (await file.read()).decode("utf-8", errors="ignore")
        count = nse_service.load_user_csv(content, is_today=isToday)
        return {
            "success": True,
            "filename": file.filename,
            "isToday": isToday,
            "contractsLoaded": count
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/tv/status")
@app.get("/api/tv")
def tv_status(action: str = "status", symbol: str = "NSE:NIFTY"):
    return {
        "status": "online",
        "provider": "TradingView-API (@mathieuc/tradingview)",
        "gateway": "Node.js + Python Fallback",
        "symbol": symbol,
        "action": action,
        "chartUrl": f"https://in.tradingview.com/chart/?symbol={symbol}",
        "supportedActions": ["quote", "ta", "search", "status"],
        "version": "1.0.0"
    }

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
@app.api_route("/", methods=["GET", "HEAD"])
def serve_index():
    index_file = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file, media_type="text/html")
    return {"message": "Bhavcopy Option Scanner API running."}

if __name__ == "__main__":
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=False)
