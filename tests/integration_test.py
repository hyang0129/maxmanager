#!/usr/bin/env python3
"""
Integration smoke test for maxmanager.

Uses two real profile directories (~/.claude-profiles/acct-a and acct-b)
that share the same Claude Max credentials. Tests the full plumbing:
profile discovery, usage probing, credential activation, state persistence,
and server endpoints.

Run:
    cd /workspaces/hub_1/maxmanager
    /workspaces/.venvs/maxmanager/bin/python tests/integration_test.py
"""

import asyncio
import json
import shutil
import signal
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmanager import core, models

# ---------------------------------------------------------------------------
# Config: real profile dirs in the devcontainer
# ---------------------------------------------------------------------------

PROFILES_DIR = Path.home() / ".claude-profiles"
REQUIRED_PROFILES = {"acct-a", "acct-b"}

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool]] = []


def check(name: str, passed: bool, detail: str = ""):
    _results.append((name, passed))
    status = "PASS" if passed else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ---------------------------------------------------------------------------
# Setup: temp dirs for active creds + state (don't touch real ~/.claude/)
# ---------------------------------------------------------------------------

_tmp_active_dir = tempfile.mkdtemp(prefix="maxmgr_active_")
_tmp_state_dir = tempfile.mkdtemp(prefix="maxmgr_state_")

TMP_ACTIVE_CREDS = Path(_tmp_active_dir) / ".credentials.json"
TMP_STATE_FILE = Path(_tmp_state_dir) / "state.json"


def cleanup():
    shutil.rmtree(_tmp_active_dir, ignore_errors=True)
    shutil.rmtree(_tmp_state_dir, ignore_errors=True)


import atexit
atexit.register(cleanup)


# Monkeypatch core module bindings so we never write to the real ~/.claude/
core.ACTIVE_CREDS = TMP_ACTIVE_CREDS
core.STATE_FILE = TMP_STATE_FILE


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight():
    print("\n=== Preflight ===")
    if not PROFILES_DIR.is_dir():
        print(f"  ABORT: {PROFILES_DIR} does not exist. Create profiles first.")
        sys.exit(2)

    found = {p.name for p in PROFILES_DIR.iterdir() if p.is_dir()}
    missing = REQUIRED_PROFILES - found
    if missing:
        print(f"  ABORT: missing profile dirs: {missing}")
        sys.exit(2)

    for name in REQUIRED_PROFILES:
        creds = PROFILES_DIR / name / ".claude" / ".credentials.json"
        if not creds.exists():
            print(f"  ABORT: {creds} not found")
            sys.exit(2)

    print(f"  OK: profiles found at {PROFILES_DIR}")


# ---------------------------------------------------------------------------
# Check 1: Profile discovery
# ---------------------------------------------------------------------------

def test_discover_profiles() -> list[models.Profile]:
    print("\n=== Check 1: Profile Discovery ===")
    profiles = core.discover_profiles(PROFILES_DIR)

    check("discover returns 2 profiles", len(profiles) == 2, f"got {len(profiles)}")

    names = {p.name for p in profiles}
    check("profile names match", names == REQUIRED_PROFILES, f"got {names}")

    all_creds_exist = all(p.credentials_path.exists() for p in profiles)
    check("all credentials files exist", all_creds_exist)

    labels = {p.label for p in profiles}
    check("labels loaded", labels == {"Account A", "Account B"}, f"got {labels}")

    return profiles


# ---------------------------------------------------------------------------
# Check 2: Usage probing (claude CLI may not be available)
# ---------------------------------------------------------------------------

def test_probe_usage(profiles: list[models.Profile]) -> list[models.UsageSnapshot]:
    print("\n=== Check 2: Usage Probing ===")
    snapshots = []

    for profile in profiles:
        # Timeout protection: 35s per probe (CLI timeout is 30s)
        def _alarm_handler(signum, frame):
            raise TimeoutError(f"probe_usage timed out for {profile.name}")

        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(35)
        try:
            snap = core.probe_usage(profile)
            snapshots.append(snap)
            check(
                f"probe {profile.name} completes",
                isinstance(snap, models.UsageSnapshot),
            )
            has_data = snap.usage_7d is not None and snap.usage_5hr is not None
            check(
                f"probe {profile.name} returns real usage",
                has_data,
                f"7d={snap.usage_7d}, 5hr={snap.usage_5hr}"
                + ("" if has_data else " (claude CLI may not be available)"),
            )
        except TimeoutError as e:
            check(f"probe {profile.name} completes", False, str(e))
            snapshots.append(models.UsageSnapshot(
                profile_name=profile.name, usage_7d=None, usage_5hr=None,
            ))
        except Exception as e:
            check(f"probe {profile.name} completes", False, str(e))
            snapshots.append(models.UsageSnapshot(
                profile_name=profile.name, usage_7d=None, usage_5hr=None,
            ))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    return snapshots


# ---------------------------------------------------------------------------
# Check 3: Credential activation
# ---------------------------------------------------------------------------

def test_activate_credential(profiles: list[models.Profile]):
    print("\n=== Check 3: Credential Activation ===")

    core.activate_credential(profiles[0])
    check("active creds file created", TMP_ACTIVE_CREDS.exists())

    src_content = profiles[0].credentials_path.read_text()
    dst_content = TMP_ACTIVE_CREDS.read_text()
    check("content matches source", src_content == dst_content)
    check("not a symlink", not TMP_ACTIVE_CREDS.is_symlink())

    # Activate second profile and verify it overwrites
    core.activate_credential(profiles[1])
    dst_content_2 = TMP_ACTIVE_CREDS.read_text()
    src_content_2 = profiles[1].credentials_path.read_text()
    check("second activation overwrites", src_content_2 == dst_content_2)


# ---------------------------------------------------------------------------
# Check 4: State persistence
# ---------------------------------------------------------------------------

def test_state_persistence(profiles: list[models.Profile]):
    print("\n=== Check 4: State Persistence ===")

    core.write_state(profiles[0], direction=None, snapshots=[])
    check("state file created", TMP_STATE_FILE.exists())

    state = core.read_state()
    check("read_state returns SwitchState", isinstance(state, models.SwitchState))
    check(
        "active_profile matches",
        state is not None and state.active_profile == profiles[0].name,
        f"got {state.active_profile if state else None}",
    )
    check(
        "direction is None on first write",
        state is not None and state.direction is None,
    )

    # Write again with direction
    core.write_state(
        profiles[1],
        direction=(profiles[0].name, profiles[1].name),
        snapshots=[],
    )
    state2 = core.read_state()
    check(
        "updated active_profile",
        state2 is not None and state2.active_profile == profiles[1].name,
    )
    check(
        "direction roundtrip",
        state2 is not None
        and state2.direction == (profiles[0].name, profiles[1].name),
        f"got {state2.direction if state2 else None}",
    )


# ---------------------------------------------------------------------------
# Check 5: Server endpoints (in-process via ASGI transport)
# ---------------------------------------------------------------------------

def test_server_endpoints():
    print("\n=== Check 5: Server Endpoints ===")

    async def _run():
        from httpx import ASGITransport, AsyncClient

        # Neutralize background loop
        async def _noop_loop():
            await asyncio.Event().wait()

        with patch("maxmanager.server.credential_loop", _noop_loop):
            # Re-import to pick up the patched constant bindings
            from maxmanager.server import app

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # 5a: GET /health
                resp = await client.get("/health")
                check("GET /health returns 200", resp.status_code == 200)
                check(
                    "GET /health body",
                    resp.json() == {"status": "ok"},
                    f"got {resp.json()}",
                )

                # 5b: GET /status (state file exists from check 4)
                with patch("maxmanager.server.read_state", return_value=core.read_state()):
                    resp = await client.get("/status")
                check("GET /status returns 200", resp.status_code == 200)
                body = resp.json()
                check(
                    "GET /status has active_profile",
                    "active_profile" in body,
                    f"got keys: {list(body.keys())}",
                )

                # 5c: POST /trigger (mock choose_credential to avoid CLI)
                canned = {"action": "no_switch", "active": "acct-a", "trigger": None}
                with patch("maxmanager.server.choose_credential", return_value=canned):
                    resp = await client.post("/trigger")
                check("POST /trigger returns 200", resp.status_code == 200)
                check(
                    "POST /trigger response has action",
                    resp.json().get("action") == "no_switch",
                    f"got {resp.json()}",
                )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Check 6: Background loop startup
# ---------------------------------------------------------------------------

def test_background_loop_startup():
    print("\n=== Check 6: Background Loop Startup ===")

    async def _run():
        canned = {"action": "activated_current", "active": "acct-a"}

        with patch("maxmanager.server.choose_credential", return_value=canned):
            from maxmanager.server import app

            task = None
            async with app.router.lifespan_context(app) as _:
                # Give the loop a moment to start
                await asyncio.sleep(0.5)

                import maxmanager.server as srv
                task = srv._loop_task

                check("loop task created", task is not None)
                if task is not None:
                    check(
                        "loop task not failed",
                        not task.done() or task.exception() is None,
                    )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("maxmanager Integration Smoke Test")
    print("=" * 60)

    preflight()

    try:
        profiles = test_discover_profiles()
        test_probe_usage(profiles)
        test_activate_credential(profiles)
        test_state_persistence(profiles)
        test_server_endpoints()
        test_background_loop_startup()
    except Exception:
        print(f"\n  UNEXPECTED ERROR:\n{traceback.format_exc()}")
        _results.append(("unexpected_error", False))

    # Summary
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f", {failed} FAILED:")
        for name, ok in _results:
            if not ok:
                print(f"  - {name}")
    else:
        print()
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
