"""
Unit tests for maxmanager/core.py public interface.

core.py is written in parallel; tests are written against the documented
interface and mock all dependencies so each test covers exactly one function.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maxmanager.models import Profile, SwitchState, Trigger, UsageSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(name: str, base_dir: Path) -> Profile:
    """Build a minimal Profile pointing at base_dir/<name>."""
    profile_dir = base_dir / name
    claude_dir = profile_dir / ".claude"
    return Profile(
        name=name,
        label=name,
        path=profile_dir,
        credentials_path=claude_dir / ".credentials.json",
        claude_dir=claude_dir,
    )


def _make_snapshot(
    profile_name: str,
    usage_7d: float | None = 50.0,
    usage_5hr: float | None = 50.0,
    reset_at_5hr: datetime | None = None,
) -> UsageSnapshot:
    return UsageSnapshot(
        profile_name=profile_name,
        usage_7d=usage_7d,
        usage_5hr=usage_5hr,
        probed_at=datetime.now(timezone.utc),
        reset_at_5hr=reset_at_5hr,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# discover_profiles
# ===========================================================================

class TestDiscoverProfiles:

    def test_discover_profiles_returns_profiles(self, fake_profiles_dir):
        from maxmanager.core import discover_profiles

        profiles = discover_profiles(fake_profiles_dir)

        assert len(profiles) == 2
        names = {p.name for p in profiles}
        assert names == {"acct-alice", "acct-bob"}

    def test_discover_profiles_skips_missing_credentials(self, tmp_path):
        from maxmanager.core import discover_profiles

        # Directory exists but has no .credentials.json inside .claude/
        no_creds = tmp_path / "acct-empty"
        (no_creds / ".claude").mkdir(parents=True)
        # Intentionally do NOT create .credentials.json

        profiles = discover_profiles(tmp_path)

        assert profiles == []

    def test_discover_profiles_reads_label_txt(self, fake_profiles_dir):
        from maxmanager.core import discover_profiles

        profiles = discover_profiles(fake_profiles_dir)

        alice = next(p for p in profiles if p.name == "acct-alice")
        # conftest writes name.replace("acct-", "").capitalize() → "Alice"
        assert alice.label == "Alice"

    def test_discover_profiles_uses_name_when_no_label(self, tmp_path):
        from maxmanager.core import discover_profiles

        name = "acct-nolabel"
        profile_dir = tmp_path / name
        claude_dir = profile_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / ".credentials.json").write_text(json.dumps({"access_token": "x"}))
        # Intentionally omit label.txt

        profiles = discover_profiles(tmp_path)

        assert len(profiles) == 1
        assert profiles[0].label == name

    def test_discover_profiles_empty_dir(self, tmp_path):
        from maxmanager.core import discover_profiles

        profiles = discover_profiles(tmp_path)

        assert profiles == []


# ===========================================================================
# probe_usage
# ===========================================================================

class TestProbeUsage:

    def test_probe_usage_success(self, tmp_path):
        from maxmanager.core import probe_usage

        profile = _make_profile("acct-alice", tmp_path)

        mock_usage = MagicMock()
        mock_usage.five_hour_pct = 44.1
        mock_usage.seven_day_pct = 62.5

        with patch("maxmanager.core.get_usage", create=True) as mock_get:
            # Patch the import inside probe_usage
            with patch.dict("sys.modules", {"claude_usage": MagicMock(get_usage=MagicMock(return_value=mock_usage))}):
                snapshot = probe_usage(profile)

        assert snapshot.profile_name == "acct-alice"
        assert snapshot.usage_7d == pytest.approx(62.5)
        assert snapshot.usage_5hr == pytest.approx(44.1)

    def test_probe_usage_failure_returns_none(self, tmp_path):
        from maxmanager.core import probe_usage

        profile = _make_profile("acct-alice", tmp_path)

        with patch.dict("sys.modules", {"claude_usage": MagicMock(get_usage=MagicMock(side_effect=RuntimeError("probe failed")))}):
            snapshot = probe_usage(profile)

        assert snapshot.profile_name == "acct-alice"
        assert snapshot.usage_7d is None
        assert snapshot.usage_5hr is None

    def test_probe_usage_import_error(self, tmp_path):
        from maxmanager.core import probe_usage

        profile = _make_profile("acct-alice", tmp_path)

        # Simulate claude_usage not installed
        import sys
        saved = sys.modules.pop("claude_usage", None)
        try:
            with patch.dict("sys.modules", {"claude_usage": None}):
                snapshot = probe_usage(profile)
        finally:
            if saved is not None:
                sys.modules["claude_usage"] = saved

        assert snapshot.profile_name == "acct-alice"
        assert snapshot.usage_7d is None
        assert snapshot.usage_5hr is None


# ===========================================================================
# evaluate_triggers
# ===========================================================================

class TestEvaluateTriggers:

    def _profiles_and_snapshots(self, tmp_path):
        """Return (current_profile, current_snapshot, [(candidate_profile, candidate_snapshot)])."""
        current_profile = _make_profile("acct-alice", tmp_path)
        candidate_profile = _make_profile("acct-bob", tmp_path)
        return current_profile, candidate_profile

    def test_evaluate_triggers_7d_equalization(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        current_snap = _make_snapshot("acct-alice", usage_7d=75.0, usage_5hr=50.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=55.0, usage_5hr=50.0)
        # delta = 75 - 55 = 20 >= 15

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        assert trigger is not None
        assert trigger.kind == "7d_equalization"
        assert trigger.target == "acct-bob"
        assert trigger.source == "acct-alice"

    def test_evaluate_triggers_no_7d_trigger(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        current_snap = _make_snapshot("acct-alice", usage_7d=60.0, usage_5hr=50.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=55.0, usage_5hr=50.0)
        # delta = 60 - 55 = 5 < 15

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        # Should not fire a 7d trigger (may be None or a different kind)
        if trigger is not None:
            assert trigger.kind != "7d_equalization"

    def test_evaluate_triggers_5hr_ceiling(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        # current at 92% (above 90% ceiling), candidate well below
        current_snap = _make_snapshot("acct-alice", usage_7d=50.0, usage_5hr=92.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=50.0, usage_5hr=40.0)

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        assert trigger is not None
        assert trigger.kind == "5hr_ceiling"
        assert trigger.target == "acct-bob"

    def test_evaluate_triggers_5hr_all_above_ceiling(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        # Both above ceiling — should pick the lowest (acct-bob at 91%)
        current_snap = _make_snapshot("acct-alice", usage_7d=50.0, usage_5hr=95.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=50.0, usage_5hr=91.0)

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        assert trigger is not None
        assert trigger.kind == "5hr_ceiling"
        assert trigger.target == "acct-bob"

    def test_evaluate_triggers_no_trigger(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        # 7d delta = 5 < 15; 5hr = 70 < 90% — no trigger should fire
        current_snap = _make_snapshot("acct-alice", usage_7d=50.0, usage_5hr=70.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=45.0, usage_5hr=60.0)

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        assert trigger is None

    def test_evaluate_triggers_none_usage_skipped(self, tmp_path):
        from maxmanager.core import evaluate_triggers

        current_profile, candidate_profile = self._profiles_and_snapshots(tmp_path)
        # current.usage_7d is None — probe failed; 7d trigger must not fire
        current_snap = _make_snapshot("acct-alice", usage_7d=None, usage_5hr=70.0)
        candidate_snap = _make_snapshot("acct-bob", usage_7d=30.0, usage_5hr=60.0)

        trigger = evaluate_triggers(
            current_profile, current_snap, [(candidate_profile, candidate_snap)]
        )

        if trigger is not None:
            assert trigger.kind != "7d_equalization"


# ===========================================================================
# check_guards
# ===========================================================================

class TestCheckGuards:

    def _recent_state(self, minutes_ago: int = 30) -> SwitchState:
        return SwitchState(
            active_profile="acct-alice",
            switched_at=_now() - timedelta(minutes=minutes_ago),
            direction=("acct-bob", "acct-alice"),
            last_snapshots=[],
        )

    def _trigger(
        self,
        kind: str = "7d_equalization",
        source: str = "acct-alice",
        target: str = "acct-bob",
        delta: float = 20.0,
    ) -> Trigger:
        return Trigger(kind=kind, source=source, target=target, delta=delta)

    def test_check_guards_startup_bypasses_all(self):
        from maxmanager.core import check_guards

        state = self._recent_state(minutes_ago=30)  # cooldown would block
        trigger = self._trigger()

        result = check_guards(trigger=trigger, state=state, startup=True)

        assert result is True

    def test_check_guards_no_trigger_returns_false(self):
        from maxmanager.core import check_guards

        result = check_guards(trigger=None, state=None, startup=False)

        assert result is False

    def test_check_guards_cooldown_blocks(self):
        from maxmanager.core import check_guards

        state = self._recent_state(minutes_ago=30)  # 30 min < 2h cooldown
        trigger = self._trigger()

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(trigger=trigger, state=state, startup=False)

        assert result is False

    def test_check_guards_cooldown_expired_allows(self):
        from maxmanager.core import check_guards

        state = SwitchState(
            active_profile="acct-alice",
            switched_at=_now() - timedelta(hours=3),  # 3h > 2h cooldown
            direction=("acct-alice", "acct-bob"),      # not a reversal for bob→alice
            last_snapshots=[],
        )
        trigger = self._trigger(source="acct-alice", target="acct-bob")
        snapshot = _make_snapshot("acct-alice", usage_5hr=50.0)

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=state, startup=False, current_usage=snapshot
            )

        assert result is True

    def test_check_guards_5hr_reset_imminent(self):
        from maxmanager.core import check_guards

        state = SwitchState(
            active_profile="acct-alice",
            switched_at=_now() - timedelta(hours=3),
            direction=None,
            last_snapshots=[],
        )
        trigger = self._trigger(kind="5hr_ceiling")
        # Reset in 15 minutes — within 30-minute proximity window
        snapshot = _make_snapshot(
            "acct-alice",
            usage_5hr=95.0,
            reset_at_5hr=_now() + timedelta(minutes=15),
        )

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=state, startup=False, current_usage=snapshot
            )

        assert result is False

    def test_check_guards_5hr_reset_not_imminent(self):
        from maxmanager.core import check_guards

        state = SwitchState(
            active_profile="acct-alice",
            switched_at=_now() - timedelta(hours=3),
            direction=None,
            last_snapshots=[],
        )
        trigger = self._trigger(kind="5hr_ceiling")
        # Reset in 60 minutes — outside 30-minute proximity window
        snapshot = _make_snapshot(
            "acct-alice",
            usage_5hr=95.0,
            reset_at_5hr=_now() + timedelta(minutes=60),
        )

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=state, startup=False, current_usage=snapshot
            )

        # Not blocked by this particular guard (may pass overall)
        # We verify that "reset imminence" alone isn't blocking here by checking
        # the guard doesn't unconditionally return False for a 60-min horizon.
        # If the function returns False it must be for a different reason.
        # Since cooldown is expired, CLI is inactive, no reversal — should be True.
        assert result is True

    def test_check_guards_cli_active(self):
        from maxmanager.core import check_guards

        state = SwitchState(
            active_profile="acct-alice",
            switched_at=_now() - timedelta(hours=3),
            direction=None,
            last_snapshots=[],
        )
        trigger = self._trigger()

        with patch("maxmanager.core._is_cli_active", return_value=True):
            result = check_guards(trigger=trigger, state=state, startup=False)

        assert result is False

    def test_check_guards_direction_reversal_insufficient(self):
        from maxmanager.core import check_guards

        # Last switch was alice→bob; now trigger wants bob→alice (reversal)
        state = SwitchState(
            active_profile="acct-bob",
            switched_at=_now() - timedelta(hours=3),
            direction=("acct-alice", "acct-bob"),
            last_snapshots=[],
        )
        # Reversal trigger: bob→alice, delta=20 which is < 25+15=40
        trigger = self._trigger(source="acct-bob", target="acct-alice", delta=20.0)
        snapshot = _make_snapshot("acct-bob", usage_5hr=50.0)

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=state, startup=False, current_usage=snapshot
            )

        assert result is False

    def test_check_guards_direction_reversal_sufficient(self):
        from maxmanager.core import check_guards

        # Same reversal scenario but delta=45 >= DELTA_7D_THRESHOLD + DIRECTION_REVERSAL_PREMIUM = 15+25=40
        state = SwitchState(
            active_profile="acct-bob",
            switched_at=_now() - timedelta(hours=3),
            direction=("acct-alice", "acct-bob"),
            last_snapshots=[],
        )
        trigger = self._trigger(source="acct-bob", target="acct-alice", delta=45.0)
        snapshot = _make_snapshot("acct-bob", usage_5hr=50.0)

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=state, startup=False, current_usage=snapshot
            )

        assert result is True

    def test_check_guards_no_state_skips_cooldown_and_reversal(self):
        from maxmanager.core import check_guards

        trigger = self._trigger()
        snapshot = _make_snapshot("acct-alice", usage_5hr=50.0)

        with patch("maxmanager.core._is_cli_active", return_value=False):
            result = check_guards(
                trigger=trigger, state=None, startup=False, current_usage=snapshot
            )

        assert result is True


# ===========================================================================
# activate_credential
# ===========================================================================

class TestActivateCredential:

    def test_activate_credential_copies_file(self, tmp_path):
        from maxmanager.core import activate_credential

        # Set up source credentials in a profile
        profile_dir = tmp_path / "acct-alice"
        claude_dir = profile_dir / ".claude"
        claude_dir.mkdir(parents=True)
        creds_content = json.dumps({"access_token": "tok_alice"})
        src = claude_dir / ".credentials.json"
        src.write_text(creds_content)

        profile = Profile(
            name="acct-alice",
            label="Alice",
            path=profile_dir,
            credentials_path=src,
            claude_dir=claude_dir,
        )

        dest = tmp_path / "active" / ".credentials.json"

        with patch("maxmanager.core.ACTIVE_CREDS", dest):
            activate_credential(profile)

        assert dest.exists(), "Destination credentials file was not created"
        assert dest.read_text() == creds_content, "File content does not match source"
        assert not dest.is_symlink(), "File must be a copy, not a symlink"

    def test_activate_credential_creates_parent_dir(self, tmp_path):
        from maxmanager.core import activate_credential

        # Source profile
        profile_dir = tmp_path / "acct-alice"
        claude_dir = profile_dir / ".claude"
        claude_dir.mkdir(parents=True)
        src = claude_dir / ".credentials.json"
        src.write_text(json.dumps({"access_token": "tok_alice"}))

        profile = Profile(
            name="acct-alice",
            label="Alice",
            path=profile_dir,
            credentials_path=src,
            claude_dir=claude_dir,
        )

        # Destination parent does NOT exist yet
        dest = tmp_path / "nonexistent_dir" / ".credentials.json"
        assert not dest.parent.exists()

        with patch("maxmanager.core.ACTIVE_CREDS", dest):
            activate_credential(profile)

        assert dest.parent.exists(), "Parent directory was not created"
        assert dest.exists(), "Destination credentials file was not created"


# ===========================================================================
# write_state + read_state
# ===========================================================================

class TestWriteReadState:

    def test_write_and_read_state_roundtrip(self, tmp_path):
        from maxmanager.core import read_state, write_state

        state_file = tmp_path / "maxmanager_state.json"

        alice_snap = _make_snapshot("acct-alice", usage_7d=60.0, usage_5hr=50.0)
        bob_snap = _make_snapshot("acct-bob", usage_7d=40.0, usage_5hr=20.0)

        alice_profile = _make_profile("acct-alice", tmp_path)

        with patch("maxmanager.core.STATE_FILE", state_file):
            write_state(
                profile=alice_profile,
                direction=("acct-bob", "acct-alice"),
                snapshots=[alice_snap, bob_snap],
            )
            result = read_state()

        assert result is not None
        assert result.active_profile == "acct-alice"
        assert result.direction == ("acct-bob", "acct-alice")
        assert len(result.last_snapshots) == 2

    def test_read_state_returns_none_when_missing(self, tmp_path):
        from maxmanager.core import read_state

        state_file = tmp_path / "does_not_exist.json"

        with patch("maxmanager.core.STATE_FILE", state_file):
            result = read_state()

        assert result is None

    def test_read_state_returns_none_on_corrupt_json(self, tmp_path):
        from maxmanager.core import read_state

        state_file = tmp_path / "maxmanager_state.json"
        state_file.write_text("this is not valid JSON }{{{")

        with patch("maxmanager.core.STATE_FILE", state_file):
            result = read_state()

        assert result is None


# ===========================================================================
# choose_credential orchestration
# ===========================================================================

class TestChooseCredential:

    def _alice_profile(self, tmp_path: Path) -> Profile:
        return _make_profile("acct-alice", tmp_path)

    def _bob_profile(self, tmp_path: Path) -> Profile:
        return _make_profile("acct-bob", tmp_path)

    def test_choose_credential_no_switch_when_no_trigger(self, tmp_path):
        from maxmanager.core import choose_credential

        alice = self._alice_profile(tmp_path)
        bob = self._bob_profile(tmp_path)
        snap_alice = _make_snapshot("acct-alice")
        snap_bob = _make_snapshot("acct-bob")

        with (
            patch("maxmanager.core.discover_profiles", return_value=[alice, bob]),
            patch("maxmanager.core.probe_usage", side_effect=[snap_alice, snap_bob]),
            patch("maxmanager.core.evaluate_triggers", return_value=None),
            patch("maxmanager.core.check_guards", return_value=False),
            patch("maxmanager.core.read_state", return_value=None),
            patch("maxmanager.core.activate_credential") as mock_activate,
            patch("maxmanager.core.write_state") as mock_write,
        ):
            result = choose_credential(startup=False)

        assert result["action"] == "no_switch"
        mock_activate.assert_not_called()
        mock_write.assert_not_called()

    def test_choose_credential_switches_when_trigger_and_guards_pass(self, tmp_path):
        from maxmanager.core import choose_credential

        alice = self._alice_profile(tmp_path)
        bob = self._bob_profile(tmp_path)
        snap_alice = _make_snapshot("acct-alice", usage_7d=75.0, usage_5hr=50.0)
        snap_bob = _make_snapshot("acct-bob", usage_7d=55.0, usage_5hr=30.0)

        trigger = Trigger(
            kind="7d_equalization",
            source="acct-alice",
            target="acct-bob",
            delta=20.0,
        )

        with (
            patch("maxmanager.core.discover_profiles", return_value=[alice, bob]),
            patch("maxmanager.core.probe_usage", side_effect=[snap_alice, snap_bob]),
            patch("maxmanager.core.evaluate_triggers", return_value=trigger),
            patch("maxmanager.core.check_guards", return_value=True),
            patch("maxmanager.core.read_state", return_value=None),
            patch("maxmanager.core.activate_credential") as mock_activate,
            patch("maxmanager.core.write_state") as mock_write,
        ):
            result = choose_credential(startup=False)

        assert result["action"] == "switched"
        mock_activate.assert_called_once()
        mock_write.assert_called_once()

    def test_choose_credential_no_profiles(self, tmp_path):
        from maxmanager.core import choose_credential

        with (
            patch("maxmanager.core.discover_profiles", return_value=[]),
            patch("maxmanager.core.activate_credential") as mock_activate,
        ):
            result = choose_credential(startup=False)

        assert result["action"] == "error"
        mock_activate.assert_not_called()
