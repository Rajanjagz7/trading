@echo off
echo ========================================================
echo  Scheduling Local 9:14 AM IST Pre-Market Task (Windows)
echo ========================================================
echo.
schtasks /create /tn "TradingScanner914AM" /tr "\"%~dp0run_scanner.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:14 /f
echo.
echo Scheduled successfully! This task will launch the scanner at 9:14 AM Mon-Fri.
pause
