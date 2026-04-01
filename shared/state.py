"""Read/write state.json with cross-platform file locking.

state.json lives on a bind-mounted directory shared between the Windows host
and all dev containers. All mutations go through this module to ensure
atomic reads/writes via file locking.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


DEFAULT_PROFILES_DIR = Path.home() / ".claude-profiles"
STATE_FILENAME = "state.json"
STALENESS_THRESHOLD_MS = 600_000  # 10 minutes


@dataclass
class RateLimits:
    used_percentage: float = 0.0
    resets_at: int = 0  # unix epoch seconds
    updated_at: int = 0  # epoch ms — when this snapshot was taken

    def is_stale(self, threshold_ms: int = STALENESS_THRESHOLD_MS) -> bool:
        """True if this data is older than threshold (default 10min)."""
        if self.updated_at == 0:
            return True
        return _now_ms() - self.updated_at > threshold_ms


@dataclass
class ProfileState:
    label: str = ""
    token_expires_at: int = 0  # epoch ms
    last_refreshed: int = 0  # epoch ms
    rate_limits_five_hour: Optional[RateLimits] = None
    rate_limits_seven_day: Optional[RateLimits] = None

    def is_token_fresh(self, buffer_ms: int = 7_200_000) -> bool:
        """True if access token won't expire within buffer (default 2h)."""
        return self.token_expires_at > _now_ms() + buffer_ms

    def five_hour_usage(self) -> float:
        """Return 5h usage %, accounting for staleness.

        Returns 0 if: the window has reset, OR the data is older than 10min.
        Stale data is treated as unknown (0%) so it doesn't bias selection
        against accounts that simply haven't reported recently.
        """
        if self.rate_limits_five_hour:
            if self.rate_limits_five_hour.is_stale():
                return 0.0
            if self.rate_limits_five_hour.resets_at and self.rate_limits_five_hour.resets_at < _now_ms() // 1000:
                return 0.0
            return self.rate_limits_five_hour.used_percentage
        return 0.0

    def seven_day_usage(self) -> float:
        """Return 7d usage %, accounting for staleness."""
        if self.rate_limits_seven_day:
            if self.rate_limits_seven_day.is_stale():
                return 0.0
            if self.rate_limits_seven_day.resets_at and self.rate_limits_seven_day.resets_at < _now_ms() // 1000:
                return 0.0
            return self.rate_limits_seven_day.used_percentage
        return 0.0

    def to_dict(self) -> dict:
        d: dict = {
            "label": self.label,
            "token_expires_at": self.token_expires_at,
            "last_refreshed": self.last_refreshed,
            "rate_limits": {},
        }
        if self.rate_limits_five_hour:
            d["rate_limits"]["five_hour"] = asdict(self.rate_limits_five_hour)
        if self.rate_limits_seven_day:
            d["rate_limits"]["seven_day"] = asdict(self.rate_limits_seven_day)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ProfileState:
        rl = d.get("rate_limits", {})
        five = rl.get("five_hour")
        seven = rl.get("seven_day")
        return cls(
            label=d.get("label", ""),
            token_expires_at=d.get("token_expires_at", 0),
            last_refreshed=d.get("last_refreshed", 0),
            rate_limits_five_hour=RateLimits(**five) if five else None,
            rate_limits_seven_day=RateLimits(**seven) if seven else None,
        )


@dataclass
class Allocation:
    container_id: str
    account: str
    claimed_at: int  # epoch ms
    pid: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Allocation:
        return cls(**d)


@dataclass
class State:
    profiles: dict[str, ProfileState] = field(default_factory=dict)
    allocations: list[Allocation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "profiles": {k: v.to_dict() for k, v in self.profiles.items()},
            "allocations": [a.to_dict() for a in self.allocations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> State:
        profiles = {
            k: ProfileState.from_dict(v)
            for k, v in d.get("profiles", {}).items()
        }
        allocations = [
            Allocation.from_dict(a) for a in d.get("allocations", [])
        ]
        return cls(profiles=profiles, allocations=allocations)

    def pick_account(self, exclude: set[str] | None = None) -> Optional[str]:
        """Pick the account with the lowest 5-hour usage.

        If exclude is provided, skip those account names.
        Returns None if no profiles are available.
        """
        candidates = [
            (name, ps)
            for name, ps in self.profiles.items()
            if exclude is None or name not in exclude
        ]
        if not candidates:
            return None

        # Sort by 5h usage ascending, then by 7d usage as tiebreaker
        candidates.sort(key=lambda x: (x[1].five_hour_usage(), x[1].seven_day_usage()))
        return candidates[0][0]

    def allocation_counts(self) -> dict[str, int]:
        """Count how many containers are allocated to each account."""
        counts: dict[str, int] = {}
        for a in self.allocations:
            counts[a.account] = counts.get(a.account, 0) + 1
        return counts

    def pick_account_balanced(self) -> Optional[str]:
        """Pick account balancing both rate limit usage and allocation count.

        Prefers the account with fewer active allocations. Breaks ties
        with lower 5-hour usage.
        """
        if not self.profiles:
            return None

        counts = self.allocation_counts()
        candidates = list(self.profiles.keys())
        candidates.sort(
            key=lambda name: (
                counts.get(name, 0),
                self.profiles[name].five_hour_usage(),
                self.profiles[name].seven_day_usage(),
            )
        )
        return candidates[0]

    def claim(self, container_id: str, account: str, pid: int) -> None:
        """Add an allocation for a container."""
        # Remove any existing allocation for this container
        self.release(container_id)
        self.allocations.append(
            Allocation(
                container_id=container_id,
                account=account,
                claimed_at=_now_ms(),
                pid=pid,
            )
        )

    def release(self, container_id: str) -> None:
        """Remove allocation for a container."""
        self.allocations = [
            a for a in self.allocations if a.container_id != container_id
        ]

    def prune_stale_allocations(self, max_age_ms: int = 86_400_000) -> int:
        """Remove stale allocations. Returns count removed.

        An allocation is stale if:
        - It's older than max_age (default 24h), OR
        - Its PID is no longer running (best-effort check)
        """
        cutoff = _now_ms() - max_age_ms
        before = len(self.allocations)
        self.allocations = [
            a for a in self.allocations
            if a.claimed_at > cutoff and _pid_alive(a.pid)
        ]
        return before - len(self.allocations)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running. Returns True if unknown (safe default)."""
    try:
        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent
        return True
    except ProcessLookupError:
        return False  # PID does not exist
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return True  # unknown error, assume alive


class StateFile:
    """Thread-safe and cross-process-safe access to state.json."""

    def __init__(self, profiles_dir: Path | str = DEFAULT_PROFILES_DIR):
        self.profiles_dir = Path(profiles_dir)
        self.path = self.profiles_dir / STATE_FILENAME

    def read(self) -> State:
        """Read current state. Returns empty state if file doesn't exist."""
        if not self.path.exists():
            return State()
        try:
            with open(self.path, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return State.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return State()

    def write(self, state: State) -> None:
        """Atomically write state with exclusive lock."""
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(state.to_dict(), f, indent=2)
                f.write("\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, self.path)

    def update(self, fn) -> State:
        """Read-modify-write with exclusive lock.

        fn receives a State and should mutate it in place.
        Returns the updated state.
        """
        # Use a lockfile for the read-modify-write cycle
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        lockpath = self.path.with_suffix(".lock")
        with open(lockpath, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                state = self.read()
                fn(state)
                self.write(state)
                return state
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
