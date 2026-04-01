#!/usr/bin/env python3
"""Periodic credential rebalancer for dev containers.

Runs as a background daemon, checking every ~1 hour whether a better
account is available and swapping if the current CLI session is idle.

Usage:
    # Start in background (typically from init.sh):
    python -m maxmanager.container.rebalancer &

    # Or run a single check:
    python -m maxmanager.container.rebalancer --once
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.state import StateFile, DEFAULT_PROFILES_DIR
from shared.oauth import read_credentials
from container.agent import get_container_id, select_and_export, print_exports


# --- Configuration ---
CHECK_INTERVAL_S = 3600          # 1 hour base interval
JITTER_MAX_S = 600               # 0-10 min random jitter
SWAP_THRESHOLD = 15.0            # min usage delta (percentage points) to trigger swap
REVERSE_THRESHOLD = 25.0         # higher bar for reversing the last swap direction
COOLDOWN_S = 7200                # 2 hours after a swap before allowing another
RESET_GRACE_S = 1800             # don't swap if current account resets within 30 min
ACTIVITY_SAMPLE_INTERVAL_S = 2   # seconds between /proc/PID/io samples
ACTIVITY_SYSCR_THRESHOLD = 10    # syscr delta above this = active

ENV_FILE = Path("/tmp/maxmanager.env")
SWAP_STATE_FILE = Path("/tmp/maxmanager-rebalancer.json")


def find_claude_pids() -> list[int]:
    """Find running Claude CLI process IDs."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "native-binary/claude"],
            text=True,
        ).strip()
        return [int(p) for p in out.splitlines() if p.strip()]
    except subprocess.CalledProcessError:
        return []


def read_proc_io_syscr(pid: int) -> int | None:
    """Read syscr (system read calls) from /proc/PID/io."""
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("syscr:"):
                    return int(line.split(":")[1].strip())
    except (FileNotFoundError, PermissionError):
        pass
    return None


def has_child_processes(pid: int) -> bool:
    """Check if a PID has any child processes (tool execution)."""
    try:
        subprocess.check_output(["pgrep", "-P", str(pid)], text=True)
        return True
    except subprocess.CalledProcessError:
        return False


def has_active_connections(pid: int) -> bool:
    """Check if a PID has ESTABLISHED TCP connections to port 443."""
    try:
        tcp_path = f"/proc/{pid}/net/tcp"
        if not os.path.exists(tcp_path):
            return False
        with open(tcp_path) as f:
            for line in f:
                # Column format: sl local_address rem_address st ...
                # Port 443 = 01BB in hex, state 01 = ESTABLISHED
                parts = line.split()
                if len(parts) >= 4 and parts[3] == "01":  # ESTABLISHED
                    remote = parts[2]
                    port_hex = remote.split(":")[1] if ":" in remote else ""
                    if port_hex == "01BB":
                        return True
    except (FileNotFoundError, PermissionError):
        pass
    return False


def is_any_cli_active() -> bool:
    """Check if any Claude CLI process is actively processing.

    Uses three signals — all must be negative to declare idle:
    1. /proc/PID/io syscr delta over 2 seconds
    2. Child processes (tool execution)
    3. TCP connections to :443 (API streaming)
    """
    pids = find_claude_pids()
    if not pids:
        return False  # no CLI running = idle

    for pid in pids:
        # Check child processes (fast, no sampling needed)
        if has_child_processes(pid):
            return True

        # Check network connections
        if has_active_connections(pid):
            return True

        # Sample I/O rate
        before = read_proc_io_syscr(pid)
        if before is not None:
            time.sleep(ACTIVITY_SAMPLE_INTERVAL_S)
            after = read_proc_io_syscr(pid)
            if after is not None and (after - before) > ACTIVITY_SYSCR_THRESHOLD:
                return True

    return False


def load_swap_state() -> dict:
    """Load rebalancer state (last swap time, direction)."""
    import json
    if SWAP_STATE_FILE.exists():
        try:
            with open(SWAP_STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_swap_state(state: dict) -> None:
    import json
    with open(SWAP_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def should_swap(
    current_account: str,
    current_usage: float,
    best_account: str,
    best_usage: float,
    swap_state: dict,
    current_resets_at: int,
) -> tuple[bool, str]:
    """Determine if a credential swap should occur.

    Returns (should_swap, reason).
    """
    now = time.time()

    delta = current_usage - best_usage

    # Check minimum delta
    if delta < SWAP_THRESHOLD:
        return False, f"delta {delta:.1f}% < threshold {SWAP_THRESHOLD}%"

    # Check cooldown
    last_swap = swap_state.get("last_swap_at", 0)
    since_swap = now - last_swap
    if since_swap < COOLDOWN_S:
        remaining = (COOLDOWN_S - since_swap) / 60
        return False, f"cooldown: {remaining:.0f}min remaining"

    # Check directional asymmetry (anti-ping-pong)
    last_from = swap_state.get("last_swap_from", "")
    last_to = swap_state.get("last_swap_to", "")
    if last_to == current_account and last_from == best_account:
        # This would reverse the last swap
        if delta < REVERSE_THRESHOLD:
            return False, f"reverse swap needs {REVERSE_THRESHOLD}% delta, got {delta:.1f}%"

    # Check if current account resets soon
    if current_resets_at > 0:
        time_until_reset = current_resets_at - now
        if 0 < time_until_reset < RESET_GRACE_S:
            return False, f"current account resets in {time_until_reset / 60:.0f}min"

    return True, f"delta {delta:.1f}% >= threshold, swap {current_account} → {best_account}"


def do_rebalance_check(profiles_dir: Path, container_id: str) -> None:
    """Run a single rebalancing check."""
    sf = StateFile(profiles_dir)
    state = sf.read()

    # Find our current account
    current_account = os.environ.get("MAXMANAGER_ACCOUNT", "")
    if not current_account:
        log("no MAXMANAGER_ACCOUNT set, skipping")
        return

    if current_account not in state.profiles:
        log(f"current account '{current_account}' not in state.json, skipping")
        return

    current_ps = state.profiles[current_account]
    current_usage = current_ps.five_hour_usage()

    # Find the best alternative
    best_account = None
    best_usage = current_usage
    for name, ps in state.profiles.items():
        if name == current_account:
            continue
        usage = ps.five_hour_usage()
        if usage < best_usage:
            best_usage = usage
            best_account = name

    if not best_account:
        log(f"no better account (current: {current_account} at {current_usage:.1f}%)")
        return

    # Check swap criteria
    swap_state = load_swap_state()
    current_resets_at = (
        current_ps.rate_limits_five_hour.resets_at
        if current_ps.rate_limits_five_hour else 0
    )

    do_swap, reason = should_swap(
        current_account, current_usage,
        best_account, best_usage,
        swap_state, current_resets_at,
    )

    if not do_swap:
        log(f"no swap: {reason}")
        return

    log(f"swap candidate: {reason}")

    # Check if CLI is active
    if is_any_cli_active():
        log("CLI is active, deferring swap")
        return

    # Perform the swap
    log(f"swapping {current_account} → {best_account}")
    env_vars = select_and_export(
        profiles_dir=profiles_dir,
        container_id=container_id,
        account=best_account,
    )

    # Write new env file
    with open(ENV_FILE, "w") as f:
        for key, val in env_vars.items():
            escaped = val.replace("'", "'\\''")
            f.write(f"export {key}='{escaped}'\n")

    # Update swap state
    save_swap_state({
        "last_swap_at": time.time(),
        "last_swap_from": current_account,
        "last_swap_to": best_account,
    })

    # Update our own env for subsequent checks in this process
    for key, val in env_vars.items():
        os.environ[key] = val

    log(f"swap complete: {current_account} → {best_account} "
        f"({current_usage:.1f}% → {best_usage:.1f}%)")
    log("new conversations will use the swapped credential")


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[maxmanager rebalancer {ts}] {msg}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="maxmanager credential rebalancer")
    parser.add_argument(
        "--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
    )
    parser.add_argument("--container-id", default=None)
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single check and exit",
    )
    parser.add_argument(
        "--interval", type=int, default=CHECK_INTERVAL_S,
        help="Check interval in seconds (default: 3600)",
    )
    args = parser.parse_args()

    container_id = args.container_id or get_container_id()

    # Handle graceful shutdown
    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False
        log("shutting down")
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if args.once:
        do_rebalance_check(args.profiles_dir, container_id)
        return

    log(f"started (interval={args.interval}s, container={container_id})")

    while running:
        jitter = random.randint(0, JITTER_MAX_S)
        sleep_time = args.interval + jitter
        log(f"next check in {sleep_time // 60}min ({jitter}s jitter)")

        # Sleep in small increments so we can respond to signals
        deadline = time.time() + sleep_time
        while running and time.time() < deadline:
            time.sleep(min(10, deadline - time.time()))

        if running:
            try:
                do_rebalance_check(args.profiles_dir, container_id)
            except Exception as e:
                log(f"error: {e}")


if __name__ == "__main__":
    main()
