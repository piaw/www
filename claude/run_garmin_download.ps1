$ErrorActionPreference = 'Stop'

$envScript = Join-Path $PSScriptRoot 'garmin_env.ps1'
if (Test-Path $envScript) {
    . $envScript
}

if ([string]::IsNullOrWhiteSpace($env:GARMINTOKENS)) {
    $env:GARMINTOKENS = Join-Path $PSScriptRoot '.garmin_tokens'
}

if ($env:GARMIN_PYTHON) {
    $pythonExe = $env:GARMIN_PYTHON
    $pythonPrefixArgs = @()
}
else {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) {
        Write-Host "Python launcher 'py' was not found on PATH." -ForegroundColor Red
        exit 1
    }
    $pythonExe = $pythonLauncher.Source
    $pythonPrefixArgs = @('-3')
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$pythonProbe = @"
import sys
print(sys.executable)
print(sys.version.replace("\n", " "))
import garminconnect
print(getattr(garminconnect, "__file__", ""))
"@
$probeOutput = & $pythonExe @pythonPrefixArgs -c $pythonProbe 2>&1
$garminConnectExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($garminConnectExitCode -ne 0) {
    Write-Host "Python package 'garminconnect' is not installed for the selected Python." -ForegroundColor Red
    Write-Host ""
    Write-Host "Selected Python command:"
    Write-Host ("  " + ((@("`"$pythonExe`"") + $pythonPrefixArgs) -join ' '))
    Write-Host ""
    Write-Host "Import check output:"
    foreach ($line in $probeOutput) {
        Write-Host "  $line"
    }
    Write-Host ""
    Write-Host "Install it with:"
    Write-Host ("  " + ((@("`"$pythonExe`"") + $pythonPrefixArgs + @('-m', 'pip', 'install', 'garminconnect')) -join ' '))
    Write-Host ""
    Write-Host "If py -3 points at an old Python, set GARMIN_PYTHON to a modern python.exe path."
    exit 1
}

$requiredEnvVars = @(
    'GARMIN_EMAIL',
    'GARMIN_PASSWORD'
)

$missing = @(
    $requiredEnvVars | Where-Object {
        [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_))
    }
)

if ($missing.Count -gt 0) {
    Write-Host "Missing required Garmin environment variables:" -ForegroundColor Red
    foreach ($name in $missing) {
        Write-Host "  $name" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Edit garmin_env.ps1 with your Garmin values, then run:"
    Write-Host "  .\run_garmin_download.ps1"
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot 'garmin_download.py'

Push-Location $PSScriptRoot
try {
    & $pythonExe @pythonPrefixArgs $scriptPath @args
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
