#!/usr/bin/env python3
"""Host manager: keeps credential profiles fresh.

Runs on the Windows host as a scheduled task or manually.
Checks each profile's token expiry and refreshes if needed.

Usage:
    # Refresh all profiles:
    python -m maxmanager.host.manager refresh

    # Check status:
    python -m maxmanager.host.manager status

    # Initial setup (interactive):
    python -m maxmanager.host.manager setup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.state import StateFile, ProfileState, DEFAULT_PROFILES_DIR
from shared.oauth import read_credentials, write_credentials, refresh_token, Credentials


REFRESH_BUFFER_MS = 7_200_000  # 2 hours before expiry


def discover_profiles(profiles_dir: Path) -> list[str]:
    """Find all profile directories (contain .credentials.json)."""
    if not profiles_dir.exists():
        return []
    return [
        d.name
        for d in sorted(profiles_dir.iterdir())
        if d.is_dir() and (d / ".credentials.json").exists()
    ]


def refresh_profile(profiles_dir: Path, name: str) -> tuple[bool, str]:
    """Refresh a single profile's tokens. Returns (success, message)."""
    creds_path = profiles_dir / name / ".credentials.json"
    creds = read_credentials(creds_path)
    if not creds:
        return False, f"No credentials at {creds_path}"

    now_ms = int(time.time() * 1000)

    if creds.expires_at > now_ms + REFRESH_BUFFER_MS:
        remaining_h = (creds.expires_at - now_ms) / 3_600_000
        return True, f"Token still fresh ({remaining_h:.1f}h remaining), skipping"

    try:
        new_creds = refresh_token(creds.refresh_token)
        # Preserve fields that the token response may not include
        if not new_creds.subscription_type:
            new_creds.subscription_type = creds.subscription_type
        if not new_creds.rate_limit_tier:
            new_creds.rate_limit_tier = creds.rate_limit_tier
        if not new_creds.scopes:
            new_creds.scopes = creds.scopes
        new_creds.organization_uuid = creds.organization_uuid

        write_credentials(creds_path, new_creds)
        return True, f"Refreshed (expires in {new_creds.expires_in_ms / 3_600_000:.1f}h)"
    except Exception as e:
        return False, f"Refresh failed: {e}"


def refresh_all(profiles_dir: Path) -> None:
    """Refresh all profiles and update state.json."""
    sf = StateFile(profiles_dir)
    profiles = discover_profiles(profiles_dir)

    if not profiles:
        print(f"No profiles found in {profiles_dir}")
        return

    for name in profiles:
        success, msg = refresh_profile(profiles_dir, name)
        status = "OK" if success else "FAIL"
        print(f"  [{status}] {name}: {msg}")

        # Update state.json with freshness info
        creds = read_credentials(profiles_dir / name / ".credentials.json")
        label_path = profiles_dir / name / "label.txt"
        label = label_path.read_text().strip() if label_path.exists() else name

        def update_profile(state, _name=name, _creds=creds, _label=label):
            if _name not in state.profiles:
                state.profiles[_name] = ProfileState()
            ps = state.profiles[_name]
            ps.label = _label
            if _creds:
                ps.token_expires_at = _creds.expires_at
                ps.last_refreshed = int(time.time() * 1000)

        sf.update(update_profile)


def show_status(profiles_dir: Path) -> None:
    """Show status of all profiles."""
    sf = StateFile(profiles_dir)
    state = sf.read()
    profiles = discover_profiles(profiles_dir)
    now_ms = int(time.time() * 1000)

    print(f"Profiles directory: {profiles_dir}")
    print(f"Profiles found: {len(profiles)}\n")

    for name in profiles:
        creds = read_credentials(profiles_dir / name / ".credentials.json")
        label_path = profiles_dir / name / "label.txt"
        label = label_path.read_text().strip() if label_path.exists() else ""

        print(f"  {name}:")
        if label:
            print(f"    Label:        {label}")
        if creds:
            remaining_ms = creds.expires_at - now_ms
            remaining_h = remaining_ms / 3_600_000
            status = "FRESH" if remaining_ms > REFRESH_BUFFER_MS else "NEEDS REFRESH" if remaining_ms > 0 else "EXPIRED"
            print(f"    Status:       {status} ({remaining_h:.1f}h remaining)")
            print(f"    Subscription: {creds.subscription_type}")
            print(f"    Tier:         {creds.rate_limit_tier}")
            print(f"    Scopes:       {', '.join(creds.scopes)}")
        else:
            print("    Status:       NO CREDENTIALS")

        ps = state.profiles.get(name)
        if ps:
            print(f"    5h usage:     {ps.five_hour_usage():.1f}%")
            print(f"    7d usage:     {ps.seven_day_usage():.1f}%")
        print()

    if state.allocations:
        print(f"Active allocations ({len(state.allocations)}):")
        for a in state.allocations:
            age_min = (now_ms - a.claimed_at) / 60_000
            print(f"  {a.container_id} → {a.account} ({age_min:.0f}m ago)")
    else:
        print("No active allocations.")


def setup_profile(profiles_dir: Path, name: str) -> None:
    """Set up a new profile from the current ~/.claude/.credentials.json."""
    source = Path.home() / ".claude" / ".credentials.json"
    if not source.exists():
        print(f"No credentials at {source}")
        print("Run 'claude auth login' first, then re-run this command.")
        return

    creds = read_credentials(source)
    if not creds:
        print(f"Could not parse {source}")
        return

    dest_dir = profiles_dir / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    write_credentials(dest_dir / ".credentials.json", creds)

    email = input(f"Label for {name} (e.g. email): ").strip()
    if email:
        (dest_dir / "label.txt").write_text(email + "\n")

    print(f"Saved profile '{name}' at {dest_dir}")
    print(f"  Subscription: {creds.subscription_type}")
    print(f"  Expires in:   {creds.expires_in_ms / 3_600_000:.1f}h")


def main():
    parser = argparse.ArgumentParser(description="maxmanager host manager")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help="Path to profiles directory",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("refresh", help="Refresh all profile tokens")
    sub.add_parser("status", help="Show profile status")

    setup_p = sub.add_parser("setup", help="Set up a new profile from current login")
    setup_p.add_argument("name", help="Profile name (e.g. account1)")

    args = parser.parse_args()

    if args.command == "refresh":
        refresh_all(args.profiles_dir)
    elif args.command == "status":
        show_status(args.profiles_dir)
    elif args.command == "setup":
        setup_profile(args.profiles_dir, args.name)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
