# Registers a Windows scheduled task that starts the bot in the background
# whenever you log in, and restarts it if it crashes.
$ErrorActionPreference = "Stop"
$TaskName = "MusicBot"
$Pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Pythonw)) { throw "No .venv found. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }
if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) { throw "No .env found. Copy .env.example to .env and add your bot token." }

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument '"bot.py"' -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"  # give the network a moment after login
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Discord music bot ($PSScriptRoot)" -Force | Out-Null

Write-Host "Autostart enabled. The bot will start in the background each time you log in."
Write-Host "Start it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:          $PSScriptRoot\bot.log"
