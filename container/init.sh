#!/bin/bash
# maxmanager container init — run in devcontainer post-create or post-start hook.
#
# Selects credentials and writes env exports to a file that the shell
# sources on every new terminal. This ensures every Claude CLI process
# in this container uses the assigned account.
#
# Usage in devcontainer.json:
#   "postStartCommand": "bash /workspaces/hub_1/maxmanager/container/init.sh"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="/tmp/maxmanager.env"

# Run the agent to select credentials
python3 "$REPO_DIR/container/agent.py" > "$ENV_FILE" 2>/dev/null

if [ -s "$ENV_FILE" ]; then
    # Source immediately for this shell
    source "$ENV_FILE"

    # Append sourcing to shell profiles so new terminals pick it up
    HOOK='[ -f /tmp/maxmanager.env ] && source /tmp/maxmanager.env'
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] && ! grep -qF "maxmanager.env" "$rc"; then
            echo "" >> "$rc"
            echo "# maxmanager credential injection" >> "$rc"
            echo "$HOOK" >> "$rc"
        fi
    done

    echo "maxmanager: credentials loaded for account ${MAXMANAGER_ACCOUNT:-unknown} (${MAXMANAGER_LABEL:-})"

    # Start the rebalancer daemon in the background
    REBALANCER_PID_FILE="/tmp/maxmanager-rebalancer.pid"
    if [ -f "$REBALANCER_PID_FILE" ] && kill -0 "$(cat "$REBALANCER_PID_FILE")" 2>/dev/null; then
        echo "maxmanager: rebalancer already running (pid $(cat "$REBALANCER_PID_FILE"))"
    else
        nohup python3 "$REPO_DIR/container/rebalancer.py" \
            > /tmp/maxmanager-rebalancer.log 2>&1 &
        echo $! > "$REBALANCER_PID_FILE"
        echo "maxmanager: rebalancer started (pid $!, log at /tmp/maxmanager-rebalancer.log)"
    fi
else
    echo "maxmanager: WARNING - no credentials selected (is ~/.claude-profiles/ mounted?)" >&2
fi
