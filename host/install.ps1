# install.ps1 — Register MaxManager host manager as a Windows Scheduled Task.
#
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
#
# Creates a task "MaxManager-Refresh" that runs at logon and repeats every 4 hours.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManagerScript = Join-Path $ScriptDir "manager.py"
$ProfilesDir = Join-Path $env:USERPROFILE ".claude-profiles"

if (-not (Test-Path $ManagerScript)) {
    Write-Error "manager.py not found at $ManagerScript"
    exit 1
}

# Ensure profiles directory exists
if (-not (Test-Path $ProfilesDir)) {
    New-Item -ItemType Directory -Path $ProfilesDir | Out-Null
    Write-Host "Created $ProfilesDir"
}

$TaskName = "MaxManager-Refresh"
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Install Python 3.10+ and try again."
    exit 1
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument """$ManagerScript"" refresh --profiles-dir ""$ProfilesDir""" `
    -WorkingDirectory $ScriptDir

$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn

# Repeat every 4 hours indefinitely
$TriggerLogon.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Remove existing task if present
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $TriggerLogon `
    -Settings $Settings `
    -Description "MaxManager: refreshes Claude Max OAuth tokens every 4 hours" `
    -RunLevel Limited

Write-Host ""
Write-Host "Installed scheduled task '$TaskName'"
Write-Host "  Python:       $PythonExe"
Write-Host "  Script:       $ManagerScript"
Write-Host "  Profiles:     $ProfilesDir"
Write-Host "  Schedule:     At logon, then every 4 hours"
Write-Host ""
Write-Host "To run immediately:  schtasks /Run /TN $TaskName"
Write-Host "To check status:     schtasks /Query /TN $TaskName"
Write-Host "To uninstall:        Unregister-ScheduledTask -TaskName $TaskName"
