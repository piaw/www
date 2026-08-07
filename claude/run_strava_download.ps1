$ErrorActionPreference = 'Stop'

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pythonLauncher) {
    Write-Host "Python launcher 'py' was not found on PATH." -ForegroundColor Red
    exit 1
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $pythonLauncher.Source -3 -c "import requests" *> $null
$requestsExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($requestsExitCode -ne 0) {
    Write-Host "Python package 'requests' is not installed for py -3." -ForegroundColor Red
    Write-Host "Install it with:"
    Write-Host "  py -3 -m pip install requests"
    exit 1
}

$requiredEnvVars = @(
    'STRAVA_CLIENT_ID',
    'STRAVA_CLIENT_SECRET',
    'STRAVA_REFRESH_TOKEN'
)

$missing = @(
    $requiredEnvVars | Where-Object {
        [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_))
    }
)

if ($missing.Count -gt 0) {
    Write-Host "Missing required Strava environment variables:" -ForegroundColor Red
    foreach ($name in $missing) {
        Write-Host "  $name" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Set those variables in this PowerShell session, then run:"
    Write-Host "  .\run_strava_download.ps1"
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot 'strava_download.py'

Push-Location $PSScriptRoot
try {
    & $pythonLauncher.Source -3 $scriptPath
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
