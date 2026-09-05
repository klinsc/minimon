#!/bin/bash
# Keep Claude Code's OAuth token fresh so minimon's usage bars stay live.
# Run hourly by claude-token-refresh.timer; only spends a (tiny) API call
# when the token has under 90 minutes left, so ~3 calls/day.
set -u
CLAUDE="$HOME/.local/bin/claude"

left=$(python3 - <<'PYEOF'
import json, os, time
try:
    c = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))
    print(int(c["claudeAiOauth"].get("expiresAt", 0) / 1000 - time.time()))
except Exception:
    print(0)
PYEOF
)

if [ "$left" -gt 5400 ]; then
    echo "token valid for another $((left / 60)) min - nothing to do"
    exit 0
fi

echo "token expires in $((left / 60)) min - refreshing via minimal CLI call"
out=$(timeout 120 "$CLAUDE" --model claude-haiku-4-5-20251001 -p "Reply with exactly: ok" 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
    echo "refreshed OK"
else
    echo "refresh FAILED (rc=$rc): $out"
fi
exit $rc
