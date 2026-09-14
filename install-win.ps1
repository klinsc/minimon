# minimon for Windows - installer.
#
#   powershell -ExecutionPolicy Bypass -File install-win.ps1
#
# - copies minimon-win-x64.exe (or runs the .pyw) into %LOCALAPPDATA%\minimon
# - adds a Startup shortcut so it launches at login
# - registers an hourly scheduled task that keeps the Claude token fresh
#
# Run install-win.ps1 -Uninstall to remove all of the above.
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$dest   = Join-Path $env:LOCALAPPDATA "minimon"
$startup = [Environment]::GetFolderPath("Startup")
$lnk    = Join-Path $startup "minimon.lnk"
$task   = "minimon token refresh"
$src    = Split-Path -Parent $MyInvocation.MyCommand.Path

function Remove-All {
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    schtasks /Delete /TN $task /F 2>$null | Out-Null
    Write-Host "Removed Startup shortcut and scheduled task."
    Write-Host "(left $dest in place; delete it by hand if you want it gone)"
}

if ($Uninstall) { Remove-All; return }

New-Item -ItemType Directory -Force -Path $dest | Out-Null

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

# Hourly token-refresh task (runs whether or not the user is logged in is not
# needed; interactive-only is fine and avoids storing a password).
Copy-Item (Join-Path $src "refresh-token.ps1") $dest -Force
$ps1 = Join-Path $dest "refresh-token.ps1"
$action = "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ps1`""
schtasks /Create /TN $task /TR $action /SC HOURLY /F | Out-Null
Write-Host "Scheduled task '$task' created (hourly)."

if ($targetArgs) { Start-Process $target -ArgumentList $targetArgs }
else { Start-Process $target }   # Start-Process rejects an empty -ArgumentList
Write-Host "minimon launched. It will now start automatically at login."
