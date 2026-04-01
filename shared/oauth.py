"""OAuth token operations for Claude Max credentials.

Handles reading credential files and performing token refresh against
the Anthropic OAuth endpoint.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Claude Code OAuth endpoints (extracted from CLI binary)
OAUTH_TOKEN_URL = "https://claude.ai/api/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44b0-b7b0-3ae4a3b560ec"


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: int  # epoch ms
    scopes: list[str]
    subscription_type: str
    rate_limit_tier: str
    organization_uuid: str = ""

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= int(time.time() * 1000)

    @property
    def expires_in_ms(self) -> int:
        return max(0, self.expires_at - int(time.time() * 1000))

    @property
    def scopes_str(self) -> str:
        return ",".join(self.scopes)

    def to_credentials_json(self) -> dict:
        """Serialize to .credentials.json format."""
        d = {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at,
                "scopes": self.scopes,
                "subscriptionType": self.subscription_type,
                "rateLimitTier": self.rate_limit_tier,
            }
        }
        if self.organization_uuid:
            d["organizationUuid"] = self.organization_uuid
        return d

    @classmethod
    def from_credentials_json(cls, data: dict) -> Credentials:
        """Parse from .credentials.json format."""
        oauth = data.get("claudeAiOauth", {})
        return cls(
            access_token=oauth.get("accessToken", ""),
            refresh_token=oauth.get("refreshToken", ""),
            expires_at=oauth.get("expiresAt", 0),
            scopes=oauth.get("scopes", []),
            subscription_type=oauth.get("subscriptionType", ""),
            rate_limit_tier=oauth.get("rateLimitTier", ""),
            organization_uuid=data.get("organizationUuid", ""),
        )


def read_credentials(path: Path) -> Optional[Credentials]:
    """Read credentials from a .credentials.json file."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials.from_credentials_json(data)
    except (json.JSONDecodeError, KeyError):
        return None


def write_credentials(path: Path, creds: Credentials) -> None:
    """Write credentials to a .credentials.json file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(creds.to_credentials_json(), f, indent=2)
        f.write("\n")


def refresh_token(refresh_tok: str, client_id: str = OAUTH_CLIENT_ID) -> Credentials:
    """Exchange a refresh token for new access + refresh tokens.

    Calls the Anthropic OAuth token endpoint. Returns updated Credentials.
    Raises on HTTP errors or invalid responses.
    """
    body = urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }).encode()

    req = Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # OAuth token response fields
    new_access = data["access_token"]
    new_refresh = data.get("refresh_token", refresh_tok)  # may not rotate
    expires_in = data.get("expires_in", 21600)  # default 6h in seconds
    scope_str = data.get("scope", "")

    scopes = [s.strip() for s in scope_str.split() if s.strip()] if scope_str else []

    return Credentials(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=int(time.time() * 1000) + (expires_in * 1000),
        scopes=scopes,
        subscription_type=data.get("subscription_type", ""),
        rate_limit_tier=data.get("rate_limit_tier", ""),
    )
