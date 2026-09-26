# Trading — NSE Bhavcopy & Live Option Scanner

A high-performance automated API and web scanner for the National Stock Exchange of India (NSE) F&O options market.

Deployed live on Vercel at `rajan-tradingvercel`.

---

## 🚀 Key Features

1. **⚡ Automated Bhavcopy Downloader (Daily EOD)**
   - Queries NSE's `/api/daily-reports?key=FO` to detect the latest available trading date and files.
   - Automatically downloads the official `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`.
   - Downloads and cross-references both **Today's** and **Previous Day's** bhavcopy for real-world Open Interest (OI%) and Previous Close changes.
   - Caches parsed data locally in `data_cache/` for instant subsequent loads.

2. **🔴 Live Market Scanner (Intraday Option Chains)**
   - Connects to NSE's updated v3 options pipeline (`/api/option-chain-contract-info` & `/api/option-chain-v3`).
   - Supports real-time scanning across index options (`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`) and stock derivatives (`RELIANCE`, `HDFCBANK`, etc.).
   - Configurable auto-refresh (every 15 seconds) during active trading hours (9:15 AM – 3:30 PM IST).

3. **🎯 Advanced Option Scanning Engine**
   - **Day Low in range (₹)** (e.g., ₹5.80 – ₹8.45)
   - **Previous Close in range (₹)** (e.g., ₹38.00 – ₹48.00)
   - **Open = Low (`O=L`) & Open = High (`O=H`)** within configurable tolerance
   - **Minimum Open Interest (OI)** threshold
   - **Nearest Expiry vs. All Expiries**
   - Filter by Instrument Type (Indices / Stocks) & Option Type (CE / PE / Both)

4. **📁 Manual Upload Fallback**
   - Drag and drop your own `BhavCopy_NSE_FO` CSV files anytime.

---

## 🛠️ API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web Scanner Dashboard UI |
| `GET` | `/api/status` | Current NSE market status (Open/Closed), trading dates, contracts loaded |
| `POST` | `/api/fetch-latest?force=false` | Trigger auto-download of the latest Bhavcopies from NSE |
| `GET` | `/api/symbols` | List of all available symbols in the loaded dataset |
| `POST` | `/api/scan` | Run scanner filter criteria against loaded Bhavcopy |
| `GET` | `/api/live/scan` | Real-time option chain scanner for specific symbol & expiry |
| `POST` | `/api/upload` | Upload custom CSV files (multipart form) |

---

## 💻 Local Usage

Double-click `run_scanner.bat` or run:
```powershell
python -m uvicorn api.index:app --host 0.0.0.0 --port 8000
```
Then visit:
```text
http://localhost:8000
```
