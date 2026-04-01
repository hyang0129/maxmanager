from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


@dataclass
class Profile:
    name: str               # directory name, e.g. "acct-alice"
    label: str              # from label.txt, or same as name if absent
    path: Path              # ~/.claude-profiles/acct-alice
    credentials_path: Path  # ~/.claude-profiles/acct-alice/.claude/.credentials.json
    claude_dir: Path        # ~/.claude-profiles/acct-alice/.claude


@dataclass
class UsageSnapshot:
    profile_name: str
    usage_7d: float | None      # 0.0–100.0, None if probe failed
    usage_5hr: float | None     # 0.0–100.0, None if probe failed
    probed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reset_at_5hr: datetime | None = None  # when the 5hr window resets, if known


@dataclass
class Trigger:
    kind: Literal["7d_equalization", "5hr_ceiling"]
    source: str   # profile_name of current account
    target: str   # profile_name of recommended switch target
    delta: float  # usage delta that fired this trigger


@dataclass
class SwitchState:
    active_profile: str
    switched_at: datetime
    direction: tuple[str, str] | None  # (from, to) of last switch
    last_snapshots: list[dict]         # list of serialized UsageSnapshot dicts

    def to_dict(self) -> dict:
        return {
            "active_profile": self.active_profile,
            "switched_at": self.switched_at.isoformat(),
            "direction": list(self.direction) if self.direction is not None else None,
            "last_snapshots": self.last_snapshots,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SwitchState":
        direction_raw = data.get("direction")
        direction: tuple[str, str] | None = (
            (direction_raw[0], direction_raw[1])
            if direction_raw is not None
            else None
        )
        return cls(
            active_profile=data["active_profile"],
            switched_at=datetime.fromisoformat(data["switched_at"]),
            direction=direction,
            last_snapshots=data.get("last_snapshots", []),
        )
