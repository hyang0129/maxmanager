import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from maxmanager.models import Profile, UsageSnapshot, SwitchState


@pytest.fixture
def fake_profiles_dir(tmp_path):
    """Create two fake profile directories with credentials and labels."""
    for name in ["acct-alice", "acct-bob"]:
        profile_dir = tmp_path / name
        claude_dir = profile_dir / ".claude"
        claude_dir.mkdir(parents=True)
        creds = {"access_token": f"tok_{name}", "refresh_token": f"ref_{name}"}
        (claude_dir / ".credentials.json").write_text(json.dumps(creds))
        (profile_dir / "label.txt").write_text(name.replace("acct-", "").capitalize())
    return tmp_path


@pytest.fixture
def sample_snapshots():
    now = datetime.now(timezone.utc)
    return [
        UsageSnapshot(profile_name="acct-alice", usage_7d=60.0, usage_5hr=50.0, probed_at=now),
        UsageSnapshot(profile_name="acct-bob",   usage_7d=40.0, usage_5hr=20.0, probed_at=now),
    ]


@pytest.fixture
def sample_state(sample_snapshots):
    return SwitchState(
        active_profile="acct-alice",
        switched_at=datetime.now(timezone.utc),
        direction=("acct-bob", "acct-alice"),
        last_snapshots=[s.__dict__ for s in sample_snapshots],
    )
