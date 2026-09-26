@echo off
title NSE Bhavcopy & Live Option Scanner
cd /d "%~dp0"
echo =======================================================
echo   Starting NSE Bhavcopy & Live Option Scanner API...
echo =======================================================
echo.
echo Opening browser at http://localhost:8000
start "" "http://localhost:8000"
echo.
python -m uvicorn api.index:app --host 0.0.0.0 --port 8000
pause
