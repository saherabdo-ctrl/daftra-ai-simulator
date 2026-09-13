@echo off
rem ============================================================
rem  DaftraAI-Simulator - PUBLIC one-click launcher
rem  Same as start.bat, PLUS a persistent cloudflared tunnel so
rem  your users can reach the app over HTTPS from anywhere.
rem  MODE: LOCAL + tunnel (needs this machine ON). Runs
rem  independently of any hosted copy (Serv00/VPS).
rem ============================================================
cd /d "%~dp0"

rem Local agents use a distinct name so they never
rem steal calls meant for the hosted (Serv00) agent.
set AGENT_NAME=daftra-ai-simulator-local

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Run setup first:
    echo     py -m venv .venv
    echo     .\.venv\Scripts\activate
    echo     pip install -r requirements.txt
    pause
    exit /b 1
)

where cloudflared >nul 2>nul
if errorlevel 1 (
    echo [ERROR] cloudflared not found. Install it with:
    echo     winget install --id Cloudflare.cloudflared
    pause
    exit /b 1
)

start "DaftraAI-Simulator - Agent" cmd /k ".\.venv\Scripts\activate.bat && python agent.py dev"
start "DaftraAI-Simulator - Server" cmd /k ".\.venv\Scripts\activate.bat && python server.py"

timeout /t 6 /nobreak >nul

start "DaftraAI-Simulator - Public Tunnel" cmd /k "cloudflared tunnel --url http://127.0.0.1:8000"

timeout /t 4 /nobreak >nul
start "" http://localhost:8000

echo.
echo ============================================================
echo  PUBLIC ACCESS  (zero-cost)  — MODE: LOCAL + Cloudflare
echo ============================================================
echo  In the "Public Tunnel" window you will see:
echo      https://xxxxxxxx.trycloudflare.com
echo.
echo  Share that URL with your users. They need nothing else.
echo  THIS MACHINE MUST STAY ON for this mode to work.
echo.
echo  DUAL MODE: this runs independently of any hosted copy
echo  (Serv00). Both can be online at once - local calls go to
echo  this agent, hosted calls go to the Serv00 agent.
echo.
echo  HOW UPDATES WORK: the URL stays the SAME as long as the
echo  "Public Tunnel" window stays open. When you update the app,
echo  close ONLY the "Agent" and "Server" windows and run
echo  start.bat again - users keep the same link and immediately
echo  get the new version. No deployment needed.
echo.
echo  If the machine or tunnel window restarts, a NEW URL is
echo  created - re-run this file and re-share it.
echo ============================================================
pause
