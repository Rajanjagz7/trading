@echo off
echo ========================================================
echo  Scheduling Local 9:16 AM IST Task (Windows)
echo ========================================================
echo.
schtasks /create /tn "TradingScanner916AM" /tr "\"%~dp0run_scanner.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:16 /f
echo.
echo Scheduled successfully! This task will launch the scanner at 9:16 AM Mon-Fri.
pause
