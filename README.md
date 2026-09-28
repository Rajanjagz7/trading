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

---

## 📊 All New Index Details

Install `jugaad-data` for pulling historical NSE derivatives (futures & options) data via CLI:

```bash
pip install jugaad-data
```

```text
$ jdata deriviatives --help
Usage: cli.py derivatives [OPTIONS]

  Sample usage-

  Download stock futures-

  jdata derivatives -s SBIN -f 2020-01-01 -t 2020-01-30 -e 2020-01-30 -i FUTSTK -o file_name.csv

  Download index futures-

  jdata derivatives -s NIFTY -f 2020-01-01 -t 2020-01-30 -e 2020-01-30 -i FUTIDX -o file_name.csv

  Download stock options-

  jdata derivatives -s SBIN -f 2020-01-01 -t 2020-01-30 -e 2020-01-30 -i OPTSTK -p 330 --ce -o file_name.csv

  Download index options-

  jdata derivatives -s NIFTY -f 2020-01-01 -t 2020-01-30 -e 2020-01-23 -i OPTIDX -p 11000 --pe -o file_name.csv

Options:
  -s, --symbol TEXT  Stock/Index symbol  [required]
  -f, --from TEXT    From date - yyyy-mm-dd  [required]
  -t, --to TEXT      To date - yyyy-mm-dd  [required]
  -e, --expiry TEXT  Expiry date - yyyy-mm-dd  [required]
  -i, --instru TEXT  FUTSTK - Stock futures, FUTIDX - Index Futures, OPTSTK -
                     Stock Options, OPTIDX - Index Options  [required]

  -p, --price TEXT   Strike price (Only for OPTSTK and OPTIDX)
  --ce / --pe        --ce for call and --pe for put (Only for OPTSTK and
                     OPTIDX)

  -o, --output TEXT  Full path of output file
  --help             Show this message and exit.
```

---

## 🟢 For Dhan Users

If you have a [Dhan](https://dhan.co) trading account, you can pull live option chain data directly via the official **DhanHQ API** instead of (or alongside) the NSE bhavcopy source.

**1. Get your API credentials**
- Log in at [web.dhan.co](https://web.dhan.co)
- Click your profile icon (top-right) → **Access DhanHQ APIs**
- Generate your `access-token` (JWT) and note your `client-id`

**2. Install the official Python client**
```bash
pip install dhanhq
```

**3. Fetch a live option chain**
```python
from dhanhq import DhanContext, dhanhq

dhan_context = DhanContext("client_id", "access_token")
dhan = dhanhq(dhan_context)

# Example: NIFTY option chain for a given expiry
option_chain = dhan.option_chain(
    under_security_id=13,          # NIFTY security ID
    under_exchange_segment="IDX_I",
    expiry="2026-10-30"
)
print(option_chain)
```

**Raw API reference** (if not using the Python client):

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/v2/optionchain` | Full option chain (OI, Greeks, IV, LTP, bid/ask, volume) for an underlying + expiry |
| `POST` | `/v2/optionchain/expirylist` | List of available expiry dates for an underlying |

Required headers on every request: `access-token`, `client-id`, `Content-Type: application/json`.

> ⚠️ **Rate limit:** Option Chain API allows **1 request per 3 seconds** per unique underlying/expiry — plan polling intervals accordingly (this is stricter than NSE's own live endpoints used elsewhere in this project).

Docs: [DhanHQ Option Chain API](https://dhanhq.co/docs/v2/option-chain/) · [DhanHQ-py on GitHub](https://github.com/dhan-oss/DhanHQ-py)


## 🟠 Upstox Live REST Mode

The live scanner can use Upstox REST market data without changing the existing scanner/filter engine.

Set the following server-side environment variable:

```text
UPSTOX_ACCESS_TOKEN=your_upstox_access_token
```

When configured, `/api/live/scan` uses:

- Upstox Option Chain API to obtain the selected expiry/contracts, LTP, OI and volume.
- Upstox Full Market Quotes V3 to obtain current OHLC and previous-close data.
- The existing scanner then applies the same Day Low, Previous Close, OI, option type, expiry and indicator rules.

The current frontend already refreshes `/api/live/scan` on its existing polling cycle, so this is **REST snapshot live data**, not a persistent tick WebSocket.

Check:

```text
GET /api/upstox/status
```

to verify the server can authenticate to Upstox.

Upstox access tokens have a daily validity window, so token lifecycle/OAuth should be handled separately rather than hard-coding a token in source control.
