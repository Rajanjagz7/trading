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

from .nse_service import nse_service

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
    return status

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
    - Otherwise (Mon-Fri 9:16 AM), refreshes Bhavcopy data and primes cache right after market opening.
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
        return scan_output
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Live scan error: {str(e)}")

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

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
@app.api_route("/", methods=["GET", "HEAD"])
def serve_index():
    index_file = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file, media_type="text/html")
    return {"message": "Bhavcopy Option Scanner API running."}

if __name__ == "__main__":
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=False)
