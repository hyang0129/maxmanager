#!/usr/bin/env python3
"""Container agent: selects credentials and exports env vars.

Usage:
    # Print env export commands (eval in shell):
    eval $(python -m maxmanager.container.agent)

    # Or with explicit container ID:
    eval $(python -m maxmanager.container.agent --container-id hub_3)

    # Show status:
    python -m maxmanager.container.agent --status

    # Release allocation on shutdown:
    python -m maxmanager.container.agent --release
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

# Allow running as script or module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.state import StateFile, DEFAULT_PROFILES_DIR
from shared.oauth import read_credentials


def get_container_id() -> str:
    """Derive a container ID from hostname or env."""
    # Devcontainer name is typically the workspace folder name
    return os.environ.get("DEVCONTAINER_ID", socket.gethostname())


def select_and_export(
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
    container_id: str | None = None,
    account: str | None = None,
) -> dict[str, str]:
    """Select an account and return env vars to export.

    Returns a dict of environment variable name → value.
    """
    sf = StateFile(profiles_dir)
    cid = container_id or get_container_id()

    def do_claim(state):
        state.prune_stale_allocations()

        if account:
            chosen = account
        else:
            chosen = state.pick_account_balanced()

        if not chosen:
            raise SystemExit("No profiles found in state.json. Run host setup first.")

        state.claim(cid, chosen, os.getpid())

    state = sf.update(do_claim)

    # Find which account we claimed
    our_alloc = next(a for a in state.allocations if a.container_id == cid)
    chosen = our_alloc.account

    # Read the credential file for the chosen account
    creds_path = profiles_dir / chosen / ".credentials.json"
    creds = read_credentials(creds_path)
    if not creds:
        raise SystemExit(
            f"No credentials found at {creds_path}. "
            f"Run host setup for account '{chosen}'."
        )

    env_vars = {
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": creds.refresh_token,
        "CLAUDE_CODE_OAUTH_SCOPES": creds.scopes_str,
        "MAXMANAGER_ACCOUNT": chosen,
        "MAXMANAGER_LABEL": state.profiles.get(chosen, None) and state.profiles[chosen].label or chosen,
        "MAXMANAGER_CONTAINER_ID": cid,
    }

    return env_vars


def print_exports(env_vars: dict[str, str]) -> None:
    """Print shell export commands."""
    for key, val in env_vars.items():
        # Shell-safe quoting
        escaped = val.replace("'", "'\\''")
        print(f"export {key}='{escaped}'")


def show_status(profiles_dir: Path = DEFAULT_PROFILES_DIR) -> None:
    """Print current allocation status."""
    sf = StateFile(profiles_dir)
    state = sf.read()

    print("=== maxmanager status ===\n")

    print("Profiles:")
    for name, ps in state.profiles.items():
        fresh = "FRESH" if ps.is_token_fresh() else "STALE"
        five = f"5h: {ps.five_hour_usage():.1f}%"
        seven = f"7d: {ps.seven_day_usage():.1f}%"
        print(f"  {name} ({ps.label}): {fresh} | {five} | {seven}")

    print(f"\nAllocations ({len(state.allocations)}):")
    for a in state.allocations:
        print(f"  {a.container_id} → {a.account} (pid {a.pid}, claimed {a.claimed_at})")

    counts = state.allocation_counts()
    print(f"\nDistribution: {counts}")


def release(
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
    container_id: str | None = None,
) -> None:
    """Release this container's allocation."""
    sf = StateFile(profiles_dir)
    cid = container_id or get_container_id()
    sf.update(lambda state: state.release(cid))
    print(f"Released allocation for {cid}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="maxmanager container agent")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help="Path to profiles directory",
    )
    parser.add_argument(
        "--container-id",
        help="Override container ID (default: hostname)",
    )
    parser.add_argument(
        "--account",
        help="Force a specific account instead of auto-selecting",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current status and exit",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Release this container's allocation and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output env vars as JSON instead of shell exports",
    )
    args = parser.parse_args()

    if args.status:
        show_status(args.profiles_dir)
        return

    if args.release:
        release(args.profiles_dir, args.container_id)
        return

    env_vars = select_and_export(
        profiles_dir=args.profiles_dir,
        container_id=args.container_id,
        account=args.account,
    )

    if args.json:
        json.dump(env_vars, sys.stdout, indent=2)
        print()
    else:
        print_exports(env_vars)

    # Status info to stderr so it doesn't interfere with eval
    acct = env_vars["MAXMANAGER_ACCOUNT"]
    label = env_vars["MAXMANAGER_LABEL"]
    print(f"maxmanager: selected {acct} ({label})", file=sys.stderr)


if __name__ == "__main__":
    main()
