@echo off
REM Starts the TrendBot backend + Telegram dashboard. Keep this window open (or run it from Task Scheduler at startup).
cd /d "%~dp0"
py backend\run_server.py
pause
