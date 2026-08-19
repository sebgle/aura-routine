<#
    install_startup.ps1 — keep aura running so it can fire by itself.

    Run once, from a normal (non-admin) PowerShell in Desktop\aura\tools\:

        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        .\install_startup.ps1

    Registers a scheduled task that starts the app at logon and restarts it if
    it ever exits. The app's own scheduler does the timing — Windows only has
    to keep the process alive.
#>

$ErrorActionPreference = "Stop"

$here     = $PSScriptRoot
$pythonw  = Join-Path $here ".venv\Scripts\pythonw.exe"   # windowless
$python   = Join-Path $here ".venv\Scripts\python.exe"
$script   = Join-Path $here "aura_web.py"
$taskName = "aura"

if (-not (Test-Path $script)) { throw "aura_web.py not found in $here" }
$exe = if (Test-Path $pythonw) { $pythonw } else { $python }
if (-not (Test-Path $exe)) { throw "venv python not found - create the venv first" }
Write-Host "using $exe" -ForegroundColor Gray

$action = New-ScheduledTaskAction -Execute $exe -Argument "`"$script`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Why these:
#   -RestartCount / -RestartInterval   bring it back if it ever dies
#   -ExecutionTimeLimit Zero           never kill it for running too long
#   -MultipleInstances IgnoreNew       never run two copies on the same port

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "registered '$taskName' - starts at logon" -ForegroundColor Green
Write-Host ""
Write-Host "Start now:  Start-ScheduledTask -TaskName $taskName"     -ForegroundColor Gray
Write-Host "Settings:   http://127.0.0.1:8770"                        -ForegroundColor Gray
Write-Host "Control:    http://127.0.0.1:8770/now"                    -ForegroundColor Gray
Write-Host "Remove:     Unregister-ScheduledTask -TaskName $taskName" -ForegroundColor Gray
Write-Host ""
Write-Host "Also do this once, or nothing fires overnight:" -ForegroundColor Yellow
Write-Host "  - Settings > System > Power: sleep and hibernate OFF"   -ForegroundColor Gray
Write-Host "  - Windows Update > Active hours: cover your sleep window" -ForegroundColor Gray
Write-Host "  - Leave the machine signed in" -ForegroundColor Gray
