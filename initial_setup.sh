#!/usr/bin/env bash
# initial_setup.sh
# Sets up N Claude Max credential profiles in ~/.claude-profiles/
# Each profile gets its own OAuth session via the claude CLI.
#
# Usage:
#   ./initial_setup.sh                        # interactive prompt
#   ./initial_setup.sh -n 3                   # acct-1, acct-2, acct-3
#   ./initial_setup.sh --names alice,bob,carol # named accounts

set -euo pipefail

PROFILES_ROOT="$HOME/.claude-profiles"

# ── Parse args ────────────────────────────────────────────────────────────────

N=0
NAMES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--count)
            N="$2"; shift 2 ;;
        --names)
            IFS=',' read -ra NAMES <<< "$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [-n COUNT] [--names name1,name2,...]"
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Resolve claude binary ─────────────────────────────────────────────────────

CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
    echo "ERROR: claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code" >&2
    exit 1
fi
echo "Found claude: $CLAUDE_BIN"

# ── Determine account names ───────────────────────────────────────────────────

if [[ ${#NAMES[@]} -gt 0 ]]; then
    ACCOUNTS=("${NAMES[@]}")
elif [[ $N -gt 0 ]]; then
    ACCOUNTS=()
    for i in $(seq 1 "$N"); do
        ACCOUNTS+=("acct-$i")
    done
else
    read -rp "How many accounts to set up? " N
    ACCOUNTS=()
    for i in $(seq 1 "$N"); do
        ACCOUNTS+=("acct-$i")
    done
fi

echo ""
echo "Will set up ${#ACCOUNTS[@]} profile(s) in: $PROFILES_ROOT"
echo "Accounts: ${ACCOUNTS[*]}"
echo ""

# ── Set up each account ───────────────────────────────────────────────────────

for name in "${ACCOUNTS[@]}"; do
    claude_dir="$PROFILES_ROOT/$name/.claude"
    creds_file="$claude_dir/.credentials.json"
    label_file="$PROFILES_ROOT/$name/label.txt"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Account: $name"

    # Skip if already authenticated
    if [[ -f "$creds_file" ]]; then
        echo "  Already has credentials — skipping."
        echo "  (Delete $creds_file to re-authenticate)"
        continue
    fi

    # Create directories
    mkdir -p "$claude_dir"

    # Write label
    if [[ ! -f "$label_file" ]]; then
        echo "$name" > "$label_file"
    fi

    echo "  Starting auth session. A browser link will appear below."
    echo "  Sign in with the Claude Max account for '$name', then return here."
    echo ""

    # Launch claude with an isolated config dir so it never touches ~/.claude.
    # Save and restore CLAUDE_CONFIG_DIR.
    saved_config_dir="${CLAUDE_CONFIG_DIR:-}"
    export CLAUDE_CONFIG_DIR="$claude_dir"

    # With no credentials in $claude_dir, the CLI will print an OAuth URL
    # and wait. The user clicks it, authenticates, and .credentials.json
    # is written into $claude_dir — never touching ~/.claude.
    "$CLAUDE_BIN" --dangerously-skip-permissions /login || true

    if [[ -n "$saved_config_dir" ]]; then
        export CLAUDE_CONFIG_DIR="$saved_config_dir"
    else
        unset CLAUDE_CONFIG_DIR
    fi

    if [[ -f "$creds_file" ]]; then
        echo ""
        echo "  Credentials saved for '$name'."
    else
        echo ""
        echo "  WARNING: credentials file not found after login for '$name'."
        echo "  Expected: $creds_file"
        echo "  You may need to re-run this script for this account."
    fi

    echo ""
done

# ── Summary ───────────────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Setup complete. Profile summary:"
echo ""

for name in "${ACCOUNTS[@]}"; do
    creds_file="$PROFILES_ROOT/$name/.claude/.credentials.json"
    if [[ -f "$creds_file" ]]; then
        echo "  [OK] $name"
    else
        echo "  [MISSING] $name — re-run to authenticate"
    fi
done

echo ""
echo "Profiles root: $PROFILES_ROOT"
