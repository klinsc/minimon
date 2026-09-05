# Keep Claude Code's OAuth token fresh so minimon's usage bars stay live.
# Windows counterpart of refresh-token.sh. Registered as a scheduled task by
# install-win.ps1 (hourly); only spends a tiny API call when the token has
# under 90 minutes left.
$ErrorActionPreference = "Stop"
$cred = Join-Path $env:USERPROFILE ".claude\.credentials.json"

function TokenSecondsLeft {
    try {
        $j = Get-Content $cred -Raw | ConvertFrom-Json
        $ms = [double]$j.claudeAiOauth.expiresAt
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        return [int](($ms - $now) / 1000)
    } catch { return 0 }
}

$left = TokenSecondsLeft
if ($left -gt 5400) {
    Write-Output "token valid for another $([int]($left/60)) min - nothing to do"
    exit 0
}

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Output "claude CLI not found on PATH - cannot refresh"
    exit 1
}

Write-Output "token expires in $([int]($left/60)) min - refreshing"
& claude --model claude-haiku-4-5-20251001 -p "Reply with exactly: ok" | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Output "refreshed OK" }
else { Write-Output "refresh FAILED (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
