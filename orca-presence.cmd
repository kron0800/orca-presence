@echo off
rem ---------------------------------------------------------------------------
rem orca-presence launcher (cmd.exe twin of orca-presence.ps1).
rem
rem Everything is resolved relative to THIS script, so it works no matter where
rem it is invoked from and in every checkout (worktree or main clone).
rem
rem Interpreter order: .venv-check, then .venv, then python on PATH.
rem The daemon runs as "python -m orca_presence.main" from the repository root.
rem
rem Usage:
rem   orca-presence.cmd              start the daemon
rem   orca-presence.cmd --dry-run    print the resolved paths and exit
rem ---------------------------------------------------------------------------
setlocal EnableExtensions

set "ROOT=%~dp0"

set "PY=%ROOT%.venv-check\Scripts\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%ROOT%"

if /I "%~1"=="--dry-run" (
    echo root     : %ROOT%
    echo python   : %PY%
    echo env file : %ROOT%.env
    exit /b 0
)

"%PY%" -m orca_presence.main %*
exit /b %ERRORLEVEL%
