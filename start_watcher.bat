@echo off
REM ============================================================
REM Mpay chain watcher launcher (Windows CMD)
REM Reads the shared config from .env (same values the API uses)
REM and starts the Go watcher with the right env vars.
REM
REM Prerequisite: the API must be running on :8000 first.
REM ============================================================
cd /d "%~dp0"

REM --- Pull shared values out of .env (comments/blank lines ignored) ---
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="RPC_URL"           set "RPC_URL=%%B"
    if /i "%%A"=="RECEIVING_ADDRESS" set "RECEIVING_ADDRESS=%%B"
    if /i "%%A"=="USDC_CONTRACT"     set "USDC_CONTRACT=%%B"
    if /i "%%A"=="INTERNAL_API_KEY"  set "INTERNAL_API_KEY=%%B"
)

REM --- Watcher-specific settings (edit here if needed) ---
set "API_URL=http://127.0.0.1:8000"
set "CONFIRMATIONS=3"
set "POLL_SECONDS=10"
set "STATE_FILE=watcher\state.json"
REM Alchemy FREE tier caps eth_getLogs at 10 blocks per call. Raise this
REM (up to ~2000) only if you upgrade the RPC plan.
set "MAX_BLOCKS=10"

REM --- Sanity check before launching ---
if "%RPC_URL%"==""           goto missing
if "%RECEIVING_ADDRESS%"=="" goto missing
if "%INTERNAL_API_KEY%"==""  goto missing

echo [start_watcher] RPC_URL, RECEIVING_ADDRESS, USDC_CONTRACT, INTERNAL_API_KEY loaded from .env
echo [start_watcher] API_URL=%API_URL%  CONFIRMATIONS=%CONFIRMATIONS%  POLL_SECONDS=%POLL_SECONDS%
echo [start_watcher] starting watcher...
watcher\mpay-watcher.exe
goto :eof

:missing
echo [start_watcher] ERROR: missing values from .env ^(RPC_URL / RECEIVING_ADDRESS / INTERNAL_API_KEY^)
exit /b 1
