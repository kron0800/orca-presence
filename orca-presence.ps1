<#
.SYNOPSIS
    Location-aware launcher for the orca-presence daemon.

.DESCRIPTION
    Resolves every path relative to THIS script instead of the current working
    directory, so the launcher works no matter where it is invoked from (or
    which checkout it lives in).

    The interpreter is picked in this order:
        1. .venv-check\Scripts\python.exe   (development environment)
        2. .venv\Scripts\python.exe         (packaged environment)
        3. python                           (whatever is on PATH)

    The daemon is then started with "python -m orca_presence.main" from the
    repository root, so the local source tree and the local .env file are the
    ones actually used (no global install involved).

.PARAMETER DryRun
    Print the resolved root, interpreter, package location and .env path, then
    exit without starting the daemon.

.EXAMPLE
    .\orca-presence.ps1
.EXAMPLE
    .\orca-presence.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DaemonArgs
)

$ErrorActionPreference = 'Stop'

# 1. The script's own directory - never the caller's working directory.
$root = $PSScriptRoot

# 2. Interpreter: local venv first, PATH last.
$candidates = @(
    (Join-Path $root '.venv-check\Scripts\python.exe'),
    (Join-Path $root '.venv\Scripts\python.exe')
)
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { $python = 'python' }

# 3. Run from the repository root so ".env" and the local package resolve.
Set-Location -LiteralPath $root

if ($DryRun) {
    $module = & $python -c "import orca_presence, os; print(os.path.dirname(orca_presence.__file__))" 2>&1
    $envFile = Join-Path $root '.env'
    Write-Host "root     : $root"
    Write-Host "python   : $python"
    Write-Host "package  : $module"
    Write-Host "env file : $(if (Test-Path -LiteralPath $envFile) { $envFile } else { '(none - environment variables only)' })"
    exit 0
}

& $python -m orca_presence.main @DaemonArgs
exit $LASTEXITCODE
