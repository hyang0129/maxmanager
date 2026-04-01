"""Integration tests for maxmanager FastAPI server endpoints."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from maxmanager.models import SwitchState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_state_dict() -> dict:
    state = SwitchState(
        active_profile="acct-alice",
        switched_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        direction=("acct-bob", "acct-alice"),
        last_snapshots=[
            {"profile_name": "acct-alice", "usage_7d": 60.0, "usage_5hr": 50.0},
            {"profile_name": "acct-bob", "usage_7d": 40.0, "usage_5hr": 20.0},
        ],
    )
    return state.to_dict()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def no_background_loop():
    """Prevent the background credential_loop from starting during tests."""
    async def _noop_loop():
        # Block forever without doing anything (cancelled on shutdown)
        await asyncio.Event().wait()

    with patch("maxmanager.server.credential_loop", _noop_loop):
        yield


@pytest_asyncio.fixture
async def client(no_background_loop):
    """Yield an async httpx client bound to the FastAPI app."""
    from maxmanager.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===========================================================================
# GET /health
# ===========================================================================

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_returns_ok_status(self, client):
        resp = await client.get("/health")
        assert resp.json() == {"status": "ok"}


# ===========================================================================
# GET /status
# ===========================================================================

class TestStatusEndpoint:

    @pytest.mark.asyncio
    async def test_status_returns_200(self, client):
        with patch("maxmanager.server.read_state", return_value=None):
            resp = await client.get("/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_status_no_state(self, client):
        with patch("maxmanager.server.read_state", return_value=None):
            resp = await client.get("/status")
        data = resp.json()
        assert data["active_profile"] is None
        assert data["last_snapshots"] == []

    @pytest.mark.asyncio
    async def test_status_with_state(self, client):
        state = SwitchState(
            active_profile="acct-alice",
            switched_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            direction=("acct-bob", "acct-alice"),
            last_snapshots=[
                {"profile_name": "acct-alice", "usage_7d": 60.0, "usage_5hr": 50.0},
            ],
        )
        with patch("maxmanager.server.read_state", return_value=state):
            resp = await client.get("/status")

        data = resp.json()
        assert data["active_profile"] == "acct-alice"
        assert data["direction"] == ["acct-bob", "acct-alice"]
        assert len(data["last_snapshots"]) == 1


# ===========================================================================
# POST /trigger
# ===========================================================================

class TestTriggerEndpoint:

    @pytest.mark.asyncio
    async def test_trigger_returns_200(self, client):
        result = {"action": "no_switch", "active": "acct-alice", "trigger": None}
        with patch("maxmanager.server.choose_credential", return_value=result):
            resp = await client.post("/trigger")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_trigger_calls_choose_credential(self, client):
        result = {"action": "switched", "from": "acct-alice", "to": "acct-bob",
                  "trigger": "7d_equalization", "delta": 20.0}
        with patch("maxmanager.server.choose_credential", return_value=result) as mock_cc:
            resp = await client.post("/trigger")

        mock_cc.assert_called_once_with(False)
        assert resp.json()["action"] == "switched"

    @pytest.mark.asyncio
    async def test_trigger_no_switch(self, client):
        result = {"action": "no_switch", "active": "acct-alice", "trigger": None}
        with patch("maxmanager.server.choose_credential", return_value=result):
            resp = await client.post("/trigger")

        assert resp.json()["action"] == "no_switch"
