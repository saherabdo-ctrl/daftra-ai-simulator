@echo off
rem ============================================
rem  SDR AI Training Lab - one-click launcher
rem  Double-click this file to start everything.
rem  MODE: LOCAL (laptop). Hosted mode (Serv00/VPS)
rem  can run at the same time without conflict:
rem  each mode uses its own agent name in LiveKit.
rem ============================================
cd /d "%~dp0"

rem Local agents use a distinct name so they never
rem steal calls meant for the hosted (Serv00) agent.
set AGENT_NAME=sdr-training-agent-local

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Run setup first:
    echo     py -m venv .venv
    echo     .\.venv\Scripts\activate
    echo     pip install -r requirements.txt
    pause
    exit /b 1
)

start "SDR AI Training Lab - Agent" cmd /k ".\.venv\Scripts\activate.bat && python agent.py dev"
start "SDR AI Training Lab - Server" cmd /k ".\.venv\Scripts\activate.bat && python server.py"

timeout /t 5 /nobreak >nul
start "" http://localhost:8000
