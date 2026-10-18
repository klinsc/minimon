# minimon for Windows - installer.
#
#   powershell -ExecutionPolicy Bypass -File install-win.ps1
#
# - copies minimon-win-x64.exe (or runs the .pyw) into %LOCALAPPDATA%\minimon
# - adds a Startup shortcut so it launches at login
# - removes the "minimon token refresh" task older versions registered
#
# Run install-win.ps1 -Uninstall to remove the shortcut.
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$dest   = Join-Path $env:LOCALAPPDATA "minimon"
$startup = [Environment]::GetFolderPath("Startup")
$lnk    = Join-Path $startup "minimon.lnk"
$task   = "minimon token refresh"     # registered by older versions only
$src    = Split-Path -Parent $MyInvocation.MyCommand.Path

# Older versions ran the Claude CLI hourly from this task to keep an OAuth
# token fresh. minimon no longer reads anything Claude-related, so clear the
# task and its script out on every install and uninstall.
function Remove-LegacyTokenTask {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Write-Host "Removed legacy scheduled task '$task'."
    }
    $old = Join-Path $dest "refresh-token.ps1"
    if (Test-Path $old) { Remove-Item $old -Force }
}

function Remove-All {
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    Remove-LegacyTokenTask
    Write-Host "Removed Startup shortcut."
    Write-Host "(left $dest in place; delete it by hand if you want it gone)"
}

if ($Uninstall) { Remove-All; return }

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Remove-LegacyTokenTask

# Prefer the packaged exe; fall back to launching the script with pythonw.
$exe = Join-Path $src "minimon-win-x64.exe"
if (Test-Path $exe) {
    Copy-Item $exe $dest -Force
    $target = Join-Path $dest "minimon-win-x64.exe"
    $targetArgs = ""
} else {
    Copy-Item (Join-Path $src "minimon-win.pyw") $dest -Force
    Copy-Item (Join-Path $src "minimon_core.py") $dest -Force
    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue)
    if (-not $pyw) { throw "No exe and no pythonw on PATH. Install Python or use the exe." }
    $target = $pyw.Source
    $targetArgs = '"' + (Join-Path $dest "minimon-win.pyw") + '"'
}

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $target
$s.Arguments = $targetArgs   # set even when empty, to clear a stale arg on an existing .lnk
$s.WorkingDirectory = $dest
$s.WindowStyle = 7
$s.Save()
Write-Host "Startup shortcut -> $target $targetArgs"

if ($targetArgs) { Start-Process $target -ArgumentList $targetArgs }
else { Start-Process $target }   # Start-Process rejects an empty -ArgumentList
Write-Host "minimon launched. It will now start automatically at login."
