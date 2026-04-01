"""Tests for maxmanager data models (Profile, UsageSnapshot, SwitchState)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maxmanager.models import Profile, UsageSnapshot, SwitchState


class TestProfile:
    def test_construction_all_fields(self):
        path = Path("/fake/profiles/acct-alice")
        claude_dir = path / ".claude"
        creds_path = claude_dir / ".credentials.json"

        profile = Profile(
            name="acct-alice",
            label="Alice",
            path=path,
            credentials_path=creds_path,
            claude_dir=claude_dir,
        )

        assert profile.name == "acct-alice"
        assert profile.label == "Alice"
        assert profile.path == path
        assert profile.credentials_path == creds_path
        assert profile.claude_dir == claude_dir


class TestUsageSnapshot:
    def test_construction_with_usage_values(self):
        now = datetime.now(timezone.utc)
        snap = UsageSnapshot(
            profile_name="acct-alice",
            usage_7d=75.5,
            usage_5hr=42.0,
            probed_at=now,
        )

        assert snap.profile_name == "acct-alice"
        assert snap.usage_7d == 75.5
        assert snap.usage_5hr == 42.0
        assert snap.probed_at == now
        assert snap.reset_at_5hr is None

    def test_construction_with_none_usage_probe_failed(self):
        """When a probe fails, usage fields should accept None."""
        now = datetime.now(timezone.utc)
        snap = UsageSnapshot(
            profile_name="acct-bob",
            usage_7d=None,
            usage_5hr=None,
            probed_at=now,
        )

        assert snap.usage_7d is None
        assert snap.usage_5hr is None

    def test_construction_with_reset_at_5hr(self):
        now = datetime.now(timezone.utc)
        reset_time = datetime.now(timezone.utc)
        snap = UsageSnapshot(
            profile_name="acct-alice",
            usage_7d=80.0,
            usage_5hr=95.0,
            probed_at=now,
            reset_at_5hr=reset_time,
        )

        assert snap.reset_at_5hr == reset_time


class TestSwitchState:
    def test_to_dict_produces_dict(self, sample_state):
        result = sample_state.to_dict()
        assert isinstance(result, dict)

    def test_to_dict_switched_at_is_string(self, sample_state):
        result = sample_state.to_dict()
        assert isinstance(result["switched_at"], str), (
            "switched_at must be serialized as an ISO string, not a datetime object"
        )

    def test_to_dict_switched_at_is_iso8601(self, sample_state):
        result = sample_state.to_dict()
        # Should parse back without error
        parsed = datetime.fromisoformat(result["switched_at"])
        assert isinstance(parsed, datetime)

    def test_to_dict_contains_expected_keys(self, sample_state):
        result = sample_state.to_dict()
        assert set(result.keys()) == {"active_profile", "switched_at", "direction", "last_snapshots"}

    def test_to_dict_direction_is_list(self, sample_state):
        result = sample_state.to_dict()
        assert isinstance(result["direction"], list)
        assert result["direction"] == ["acct-bob", "acct-alice"]

    def test_from_dict_roundtrip(self, sample_state):
        d = sample_state.to_dict()
        restored = SwitchState.from_dict(d)

        assert restored.active_profile == sample_state.active_profile
        assert restored.switched_at == sample_state.switched_at
        assert restored.direction == sample_state.direction
        assert restored.last_snapshots == sample_state.last_snapshots

    def test_from_dict_roundtrip_direction_is_tuple(self, sample_state):
        d = sample_state.to_dict()
        restored = SwitchState.from_dict(d)

        assert isinstance(restored.direction, tuple)
        assert restored.direction == ("acct-bob", "acct-alice")

    def test_to_dict_direction_none(self):
        state = SwitchState(
            active_profile="acct-alice",
            switched_at=datetime.now(timezone.utc),
            direction=None,
            last_snapshots=[],
        )
        result = state.to_dict()
        assert result["direction"] is None

    def test_from_dict_direction_none(self):
        state = SwitchState(
            active_profile="acct-alice",
            switched_at=datetime.now(timezone.utc),
            direction=None,
            last_snapshots=[],
        )
        d = state.to_dict()
        restored = SwitchState.from_dict(d)
        assert restored.direction is None

    def test_from_dict_roundtrip_direction_none_full(self):
        state = SwitchState(
            active_profile="acct-bob",
            switched_at=datetime.now(timezone.utc),
            direction=None,
            last_snapshots=[],
        )
        restored = SwitchState.from_dict(state.to_dict())
        assert restored.active_profile == "acct-bob"
        assert restored.direction is None
        assert restored.last_snapshots == []
