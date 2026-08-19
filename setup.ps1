<#
    setup.ps1 - one command to go from a fresh clone to a running app.

        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        .\setup.ps1

    Creates the virtual environment, installs dependencies, creates aura.toml,
    and tries to find your bulb. Safe to re-run.
#>

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Set-Location $here

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

Step 1 "Checking Python"
$py = $null
foreach ($c in @("python", "python3", "py")) {
    try {
        $v = & $c --version 2>&1
        if ($v -match "Python (\d+)\.(\d+)") {
            if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11)) {
                $py = $c; Write-Host "    $v" -ForegroundColor Gray; break
            }
        }
    } catch { }
}
if (-not $py) {
    Write-Host "    Python 3.11 or newer is required." -ForegroundColor Red
    Write-Host "    Get it from https://www.python.org/downloads/ and tick" -ForegroundColor Gray
    Write-Host "    'Add python.exe to PATH' during install." -ForegroundColor Gray
    exit 1
}

Step 2 "Creating the virtual environment"
if (Test-Path ".venv") { Write-Host "    already exists" -ForegroundColor Gray }
else { & $py -m venv .venv; Write-Host "    created .venv" -ForegroundColor Gray }
$venvPy = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "    venv creation failed" -ForegroundColor Red; exit 1 }

Step 3 "Installing dependencies (takes a minute)"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "    install failed" -ForegroundColor Red; exit 1 }
Write-Host "    done" -ForegroundColor Gray

Step 4 "Configuration"
if (Test-Path "aura.toml") { Write-Host "    aura.toml already exists, leaving it alone" -ForegroundColor Gray }
else { Copy-Item "aura.toml.example" "aura.toml"; Write-Host "    created aura.toml" -ForegroundColor Gray }

Step 5 "Checking audio"
& $venvPy aura_web.py --audio-check

Step 6 "Looking for your bulb"
Write-Host "    (enable 'Allow local communication' in the WiZ app first)" -ForegroundColor Gray
& $venvPy aura_web.py --discover

Write-Host "`n---------------------------------------------" -ForegroundColor DarkGray
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Start it:      .\.venv\Scripts\python.exe aura_web.py" -ForegroundColor White
Write-Host "Settings:      http://127.0.0.1:8770"                   -ForegroundColor Gray
Write-Host "Control:       http://127.0.0.1:8770/now"               -ForegroundColor Gray
Write-Host ""
Write-Host "Then, to have it run by itself every day:" -ForegroundColor White
Write-Host "  .\install_startup.ps1" -ForegroundColor Gray
Write-Host ""
Write-Host "And in Windows, or nothing fires overnight:" -ForegroundColor Yellow
Write-Host "  - Power settings: sleep and hibernate OFF"           -ForegroundColor Gray
Write-Host "  - Windows Update: active hours cover your sleep"     -ForegroundColor Gray
Write-Host "  - Leave the machine signed in"                       -ForegroundColor Gray
