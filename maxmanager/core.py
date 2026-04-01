"""Core credential selector logic for maxmanager."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from .constants import (
    ACTIVE_CREDS,
    COOLDOWN_HOURS,
    DELTA_7D_THRESHOLD,
    DIRECTION_REVERSAL_PREMIUM,
    PROFILES_DIR,
    RESET_PROXIMITY_MINUTES,
    SOFT_CEILING_5HR,
    STATE_FILE,
)
from .models import Profile, SwitchState, Trigger, UsageSnapshot


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_usage_output(output: str) -> tuple[float | None, float | None]:
    """Parse CLI output for usage_5hr and usage_7d percentages.

    Returns (usage_5hr, usage_7d) as percentages 0-100, or (None, None) if
    unparseable.  Tries JSON first, then falls back to regex patterns.
    """
    # Try JSON first
    try:
        data = json.loads(output.strip())
        return float(data["usage_5hr_pct"]), float(data["usage_7d_pct"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    # Fallback: regex for patterns like "5hr: 45%" or "7-day: 72%"
    usage_5hr: float | None = None
    usage_7d: float | None = None

    m5 = re.search(r"5[-\s]?hr[^\d]*?([\d.]+)\s*%", output, re.IGNORECASE)
    if m5:
        try:
            usage_5hr = float(m5.group(1))
        except ValueError:
            pass

    m7 = re.search(r"7[-\s]?day[^\d]*?([\d.]+)\s*%", output, re.IGNORECASE)
    if m7:
        try:
            usage_7d = float(m7.group(1))
        except ValueError:
            pass

    if usage_5hr is not None or usage_7d is not None:
        return usage_5hr, usage_7d

    return None, None


def _is_cli_active() -> bool:
    """Return True if the claude CLI appears to be actively running.

    Checks child processes of the current process tree OR TCP connections to
    :443.  Uses psutil if available, falls back to subprocess checks.
    """
    try:
        import psutil

        for proc in psutil.process_iter(["name", "connections"]):
            try:
                if "claude" in (proc.info["name"] or "").lower():
                    for conn in proc.info.get("connections") or []:
                        if (
                            conn.raddr
                            and conn.raddr.port == 443
                            and conn.status == "ESTABLISHED"
                        ):
                            return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except ImportError:
        # psutil not available — fall back to ss
        try:
            result = subprocess.run(
                ["ss", "-tnp", "state", "established", "dport", "443"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "claude" in result.stdout.lower()
        except Exception:
            return False  # can't determine, assume not active


def _hostname_index(n: int) -> int:
    """Return a deterministic index in [0, n) based on the local hostname."""
    digest = hashlib.sha256(socket.gethostname().encode()).hexdigest()
    return int(digest, 16) % n


# ---------------------------------------------------------------------------
# Public sub-functions
# ---------------------------------------------------------------------------


def discover_profiles(profiles_dir: Path = PROFILES_DIR) -> list[Profile]:
    """Scan *profiles_dir* and return a sorted list of valid Profile objects.

    A directory is valid if it contains ``.claude/.credentials.json``.
    The label is read from ``label.txt`` if present; otherwise the directory
    name is used.
    """
    found: list[Profile] = []

    if not profiles_dir.is_dir():
        logger.warning(f"Profiles directory does not exist: {profiles_dir}")
        return found

    for entry in profiles_dir.iterdir():
        if not entry.is_dir():
            continue

        claude_dir = entry / ".claude"
        credentials_path = claude_dir / ".credentials.json"

        if not credentials_path.exists():
            logger.warning(
                f"Profile '{entry.name}' skipped: missing {credentials_path}"
            )
            continue

        label_file = entry / "label.txt"
        label = label_file.read_text().strip() if label_file.exists() else entry.name

        found.append(
            Profile(
                name=entry.name,
                label=label,
                path=entry,
                credentials_path=credentials_path,
                claude_dir=claude_dir,
            )
        )

    found.sort(key=lambda p: p.name)
    logger.info(f"Discovered {len(found)} profile(s) in {profiles_dir}")
    return found


def probe_usage(profile: Profile) -> UsageSnapshot:
    """Invoke the claude CLI to query usage for *profile*.

    Returns a :class:`UsageSnapshot`.  On any subprocess failure the snapshot
    has ``usage_7d=None`` and ``usage_5hr=None``.
    """
    prompt = (
        "What are my current usage stats? "
        "Reply with ONLY a JSON object with keys "
        '"usage_5hr_pct" (0-100 float) and "usage_7d_pct" (0-100 float). '
        "No other text."
    )
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(profile.claude_dir)}
    cmd = ["claude", "--print", "--no-stream", prompt]

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result.check_returncode()
        usage_5hr, usage_7d = _parse_usage_output(result.stdout)
        return UsageSnapshot(
            profile_name=profile.name,
            usage_7d=usage_7d,
            usage_5hr=usage_5hr,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(f"probe_usage: timeout for profile '{profile.name}': {exc}")
    except subprocess.CalledProcessError as exc:
        logger.warning(
            f"probe_usage: CLI error for profile '{profile.name}': {exc}"
        )
    except FileNotFoundError as exc:
        logger.warning(
            f"probe_usage: claude CLI not found for profile '{profile.name}': {exc}"
        )

    return UsageSnapshot(
        profile_name=profile.name,
        usage_7d=None,
        usage_5hr=None,
    )


def evaluate_triggers(
    current: Profile,
    current_usage: UsageSnapshot,
    candidates: list[tuple[Profile, UsageSnapshot]],
) -> Trigger | None:
    """Evaluate both switch triggers in priority order.

    Returns a :class:`Trigger` if one fires, otherwise ``None``.
    """
    if not candidates:
        return None

    # ------------------------------------------------------------------
    # Trigger 1: 7-day equalization
    # ------------------------------------------------------------------
    valid_7d = [
        (p, s) for p, s in candidates if s.usage_7d is not None
    ]
    if current_usage.usage_7d is not None and valid_7d:
        min_7d = min(s.usage_7d for _, s in valid_7d)
        tied_7d = [(p, s) for p, s in valid_7d if s.usage_7d == min_7d]
        if len(tied_7d) > 1:
            best_7d_pair = tied_7d[_hostname_index(len(tied_7d))]
        else:
            best_7d_pair = tied_7d[0]

        delta_7d = current_usage.usage_7d - best_7d_pair[1].usage_7d
        if delta_7d >= DELTA_7D_THRESHOLD:
            return Trigger(
                kind="7d_equalization",
                source=current.name,
                target=best_7d_pair[0].name,
                delta=delta_7d,
            )

    # ------------------------------------------------------------------
    # Trigger 2: 5-hour soft ceiling
    # ------------------------------------------------------------------
    if (
        current_usage.usage_5hr is not None
        and current_usage.usage_5hr >= SOFT_CEILING_5HR * 100
    ):
        valid_5hr = [(p, s) for p, s in candidates if s.usage_5hr is not None]
        if valid_5hr:
            min_5hr = min(s.usage_5hr for _, s in valid_5hr)
            tied_5hr = [(p, s) for p, s in valid_5hr if s.usage_5hr == min_5hr]
            if len(tied_5hr) > 1:
                best_5hr_pair = tied_5hr[_hostname_index(len(tied_5hr))]
            else:
                best_5hr_pair = tied_5hr[0]

            return Trigger(
                kind="5hr_ceiling",
                source=current.name,
                target=best_5hr_pair[0].name,
                delta=current_usage.usage_5hr - best_5hr_pair[1].usage_5hr,
            )

    return None


def check_guards(
    trigger: Trigger | None,
    state: SwitchState | None,
    startup: bool,
    current_usage: UsageSnapshot | None = None,
) -> bool:
    """Return True if a switch should proceed, False if it should be skipped.

    When *startup* is True all guards are bypassed and True is returned
    immediately.
    """
    if startup:
        logger.info("startup: bypassing all guards")
        return True

    # Guard 1: no trigger
    if trigger is None:
        logger.debug("no trigger fired, staying on current account")
        return False

    now = datetime.now(timezone.utc)

    # Guard 2: cooldown
    if state is not None:
        switched_at = state.switched_at
        # Make switched_at timezone-aware if it isn't already
        if switched_at.tzinfo is None:
            switched_at = switched_at.replace(tzinfo=timezone.utc)
        elapsed = now - switched_at
        cooldown = timedelta(hours=COOLDOWN_HOURS)
        if elapsed < cooldown:
            elapsed_minutes = int(elapsed.total_seconds() / 60)
            logger.info(
                f"guard: cooldown active (switched {elapsed_minutes}m ago)"
            )
            return False

    # Guard 3: 5hr reset imminent
    if (
        trigger.kind == "5hr_ceiling"
        and current_usage is not None
        and current_usage.reset_at_5hr is not None
    ):
        reset_at = current_usage.reset_at_5hr
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        time_to_reset = reset_at - now
        if time_to_reset < timedelta(minutes=RESET_PROXIMITY_MINUTES):
            minutes_left = max(0, int(time_to_reset.total_seconds() / 60))
            logger.info(
                f"guard: 5hr reset imminent in {minutes_left}m, skipping"
            )
            return False

    # Guard 4: CLI active
    if _is_cli_active():
        logger.info("guard: CLI is active, skipping switch")
        return False

    # Guard 5: direction reversal
    if (
        state is not None
        and state.direction is not None
        and state.direction == (trigger.target, trigger.source)
    ):
        required_delta = DIRECTION_REVERSAL_PREMIUM + DELTA_7D_THRESHOLD
        if trigger.delta < required_delta:
            logger.info(
                "guard: direction reversal requires higher delta, skipping"
            )
            return False

    return True


def activate_credential(profile: Profile) -> None:
    """Copy *profile*'s credentials to the active credential path.

    Uses :func:`shutil.copy2` (never a symlink).  Creates parent directories
    as needed.
    """
    ACTIVE_CREDS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(profile.credentials_path, ACTIVE_CREDS)
    logger.info(f"Activated credential: {profile.name} -> {ACTIVE_CREDS}")


def write_state(
    profile: Profile,
    direction: tuple[str, str] | None,
    snapshots: list[UsageSnapshot],
) -> None:
    """Atomically write switch state to :data:`STATE_FILE`.

    Writes to a ``.tmp`` file then renames to avoid partial reads.
    """
    state = SwitchState(
        active_profile=profile.name,
        switched_at=datetime.now(timezone.utc),
        direction=direction,
        last_snapshots=[s.__dict__ for s in snapshots],
    )
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, default=str))
    tmp.rename(STATE_FILE)
    logger.info(f"State written: active={profile.name}, direction={direction}")


def read_state() -> SwitchState | None:
    """Read and return the persisted :class:`SwitchState`, or ``None``.

    Returns ``None`` if the state file is missing or corrupt.
    """
    try:
        data = json.loads(STATE_FILE.read_text())
        return SwitchState.from_dict(data)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.debug(f"No valid state file: {exc}")
        return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def choose_credential(startup: bool) -> dict:
    """Discover profiles, probe usage, evaluate triggers, apply guards, and
    activate the best credential.

    Returns a dict describing the outcome.
    """
    logger.debug(f"choose_credential(startup={startup}) called")

    profiles = discover_profiles()
    if not profiles:
        logger.error("No valid profiles found")
        return {"action": "error", "reason": "no_profiles"}

    state = read_state()

    # Determine current profile (from state, or first profile on startup)
    current_name = state.active_profile if state else profiles[0].name
    current_profile = next(
        (p for p in profiles if p.name == current_name), profiles[0]
    )

    # Probe all profiles
    all_snapshots: list[UsageSnapshot] = []
    for profile in profiles:
        snapshot = probe_usage(profile)
        all_snapshots.append(snapshot)

    current_snapshot = next(
        (s for s in all_snapshots if s.profile_name == current_profile.name),
        all_snapshots[0],
    )
    candidates = [
        (p, s)
        for p, s in zip(profiles, all_snapshots)
        if p.name != current_profile.name
    ]

    trigger = evaluate_triggers(current_profile, current_snapshot, candidates)

    if not check_guards(trigger, state, startup, current_snapshot):
        return {
            "action": "no_switch",
            "active": current_profile.name,
            "trigger": trigger.kind if trigger else None,
        }

    if trigger is None:
        activate_credential(current_profile)
        write_state(current_profile, None, all_snapshots)
        return {"action": "activated_current", "active": current_profile.name}

    # Activate the target profile
    target_profile = next(p for p in profiles if p.name == trigger.target)
    direction = (current_profile.name, target_profile.name)

    activate_credential(target_profile)
    write_state(target_profile, direction, all_snapshots)

    logger.info(
        f"Switched: {current_profile.name} -> {target_profile.name} "
        f"(trigger: {trigger.kind}, delta: {trigger.delta:.1f})"
    )

    return {
        "action": "switched",
        "from": current_profile.name,
        "to": target_profile.name,
        "trigger": trigger.kind,
        "delta": trigger.delta,
    }
