@echo off
REM =========================================================================
REM GhostWatch one-click launcher
REM Installs Python dependencies if missing, starts the FastAPI backend,
REM waits until it answers, then opens the dashboard in your browser.
REM
REM Requires: Python 3.10+ on PATH  (https://www.python.org/downloads/)
REM Keep the "GhostWatch API" window open while using the dashboard.
REM =========================================================================
setlocal
cd /d "%~dp0"

echo [GhostWatch] Checking Python dependencies...
python -c "import fastapi, uvicorn, pandas" >nul 2>&1
if errorlevel 1 (
    echo [GhostWatch] Dependencies missing - installing from backend\requirements.txt ...
    python -m pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo [GhostWatch] ERROR: dependency installation failed. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)

echo [GhostWatch] Starting backend on http://localhost:8000 ...
REM /D sets the spawned window's working directory (project path contains spaces)
start "GhostWatch API" /D "%~dp0backend" cmd /k python -m uvicorn app:app --host 127.0.0.1 --port 8000

echo [GhostWatch] Waiting for the API to come up ...
powershell -NoProfile -Command "$up=$false; for ($i=0; $i -lt 40; $i++) { try { Invoke-RestMethod http://127.0.0.1:8000/stats -TimeoutSec 1 | Out-Null; $up=$true; break } catch { Start-Sleep -Milliseconds 500 } }; if (-not $up) { exit 1 }"
if errorlevel 1 (
    echo [GhostWatch] WARNING: the API did not answer within 20s - opening the dashboard anyway.
    echo [GhostWatch]          If the dashboard shows a connection error, check the API window.
) else (
    echo [GhostWatch] API is up.
)

echo [GhostWatch] Opening dashboard ...
start "" "%~dp0frontend\index.html"
echo [GhostWatch] Done. Interactive API docs: http://localhost:8000/docs
echo [GhostWatch] To stop: close the "GhostWatch API" window (or press Ctrl+C in it).
timeout /t 3 >nul
endlocal
