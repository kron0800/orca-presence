@echo off
rem ---------------------------------------------------------------------------
rem orca-presence launcher for Task Scheduler / the Startup folder.
rem
rem Everything is resolved relative to THIS script (never the caller's working
rem directory), so the same file works in every checkout - the main clone and
rem any git worktree.
rem
rem Interpreter order: .venv-check, then .venv, then python on PATH. The daemon
rem runs as "python -m orca_presence.main" from the repository root, so the
rem local source tree and the local .env file are the ones actually used.
rem
rem Environment overrides:
rem   ORCA_PRESENCE_HOME     repository root (defaults to this script's parent)
rem   ORCA_PRESENCE_LOG_FILE file to append the daemon output to (optional)
rem ---------------------------------------------------------------------------
setlocal EnableExtensions

if not defined ORCA_PRESENCE_HOME for %%I in ("%~dp0..") do set "ORCA_PRESENCE_HOME=%%~fI"

set "PY=%ORCA_PRESENCE_HOME%\.venv-check\Scripts\python.exe"
if not exist "%PY%" set "PY=%ORCA_PRESENCE_HOME%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%ORCA_PRESENCE_HOME%"

if defined ORCA_PRESENCE_LOG_FILE (
    "%PY%" -m orca_presence.main %* >> "%ORCA_PRESENCE_LOG_FILE%" 2>&1
) else (
    "%PY%" -m orca_presence.main %*
)
exit /b %ERRORLEVEL%
