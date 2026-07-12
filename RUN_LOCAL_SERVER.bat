@echo off
cd /d "%~dp0"
echo Starting Cadence locally...
echo.
echo Keep this window open while testing.
echo App URL: http://127.0.0.1:8002/upload/
echo.
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local_server.ps1"
echo.
echo Server stopped. If there is an error above, send it to Codex.
pause
