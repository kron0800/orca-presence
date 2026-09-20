@echo off
rem ---------------------------------------------------------------------------
rem orca-presence launcher for Task Scheduler / the Startup folder.
rem
rem Runs the console script installed by "pip install -e ." from the project's
rem virtual environment and exits with the daemon's exit code.
rem
rem Environment overrides:
rem   ORCA_PRESENCE_HOME     project root (defaults to this script's parent)
rem   ORCA_PRESENCE_LOG_FILE file to append the daemon output to (optional)
rem ---------------------------------------------------------------------------
setlocal

if not defined ORCA_PRESENCE_HOME for %%I in ("%~dp0..") do set "ORCA_PRESENCE_HOME=%%~fI"

set "ORCA_PRESENCE_EXE=%ORCA_PRESENCE_HOME%\.venv\Scripts\orca-presence.exe"
if not exist "%ORCA_PRESENCE_EXE%" (
    echo [orca-presence] Virtual environment not found: "%ORCA_PRESENCE_EXE%" 1>&2
    echo [orca-presence] Create it and install the project with: 1>&2
    echo [orca-presence]     py -3.11 -m venv .venv ^&^& .venv\Scripts\python -m pip install -e . 1>&2
    exit /b 2
)

if defined ORCA_PRESENCE_LOG_FILE (
    "%ORCA_PRESENCE_EXE%" %* >> "%ORCA_PRESENCE_LOG_FILE%" 2>&1
) else (
    "%ORCA_PRESENCE_EXE%" %*
)
exit /b %ERRORLEVEL%
