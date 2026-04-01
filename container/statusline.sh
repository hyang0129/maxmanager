#!/bin/bash
# Claude Code statusline hook — reports rate limit usage and updates state.json.
#
# Configure in Claude Code settings.json:
#   "statusLine": "bash /workspaces/hub_1/maxmanager/container/statusline.sh"
#
# The CLI pipes JSON with rate_limits data to stdin on each render.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PROFILES_DIR="${HOME}/.claude-profiles"
STATE_FILE="${PROFILES_DIR}/state.json"

input=$(cat)

# Extract rate limit data
five=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null)
five_reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty' 2>/dev/null)
week=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null)
week_reset=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty' 2>/dev/null)

# Build status line output
out=""
acct="${MAXMANAGER_ACCOUNT:-?}"
[ -n "$five" ] && out="${acct} 5h:$(printf '%.0f' "$five")%"
[ -n "$week" ] && out="$out 7d:$(printf '%.0f' "$week")%"

# Update state.json with latest rate limit snapshot (best-effort, non-blocking)
if [ -n "$five" ] && [ -f "$STATE_FILE" ] && [ -n "${MAXMANAGER_ACCOUNT:-}" ]; then
    python3 -c "
import json, sys, fcntl, os
acct = os.environ.get('MAXMANAGER_ACCOUNT', '')
five = float('${five}') if '${five}' else None
five_reset = int('${five_reset}') if '${five_reset}' else None
week = float('${week}') if '${week}' else None
week_reset = int('${week_reset}') if '${week_reset}' else None

state_path = '${STATE_FILE}'
lock_path = state_path + '.lock'

try:
    with open(lock_path, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with open(state_path) as f:
                state = json.load(f)
            p = state.get('profiles', {}).get(acct, {})
            rl = p.setdefault('rate_limits', {})
            now_ms = int(__import__('time').time() * 1000)
            if five is not None:
                rl['five_hour'] = {'used_percentage': five, 'resets_at': five_reset or 0, 'updated_at': now_ms}
            if week is not None:
                rl['seven_day'] = {'used_percentage': week, 'resets_at': week_reset or 0, 'updated_at': now_ms}
            tmp = state_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, state_path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
except (BlockingIOError, FileNotFoundError, json.JSONDecodeError):
    pass  # Non-blocking: skip if locked or missing
" 2>/dev/null &
fi

echo "$out"
