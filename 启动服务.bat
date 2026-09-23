@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service_ctl.ps1" start
pause
