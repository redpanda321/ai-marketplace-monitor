"""Private per-tenant Facebook session storage and shared live verification."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
SESSION_TTL_SECONDS = 86400


def facebook_session_error(page: Any) -> str | None:
    """A cookie alone is insufficient: require authenticated Marketplace UI."""
    url = urlsplit(page.url)
    if url.scheme != "https" or not (
        url.hostname == "facebook.com" or (url.hostname or "").endswith(".facebook.com")
    ):
        return "marketplace_navigation_unverified"
    if any(
        part in url.path
        for part in ("checkpoint", "captcha", "two_step_verification", "authentication")
    ):
        return "marketplace_challenge_required"
    if (
        "/login" in url.path
        or page.locator('input[name="email"]:visible, input[name="pass"]:visible').count()
    ):
        return "marketplace_login_required"
    cookies = page.context.cookies("https://www.facebook.com")
    account = page.locator(
        '[role="button"][aria-label="Your profile"]:visible, [role="button"][aria-label="Account"]:visible, [role="button"][aria-label="Account, your profile"]:visible'
    )
    if (
        not any(cookie.get("name") == "c_user" and cookie.get("value") for cookie in cookies)
        or not account.count()
        or not page.locator('[role="navigation"]:visible').count()
    ):
        return "marketplace_session_unverified"
    if (
        not url.path.startswith("/marketplace")
        or not page.locator('[role="main"]:visible').count()
    ):
        return "marketplace_access_unverified"
    return None


def account_fingerprint(config: str) -> str:
    facebook = tomllib.loads(config).get("marketplace", {}).get("facebook", {})
    return hashlib.sha256(
        json.dumps([facebook.get("username", ""), facebook.get("password", "")]).encode()
    ).hexdigest()


def verify_facebook_session(page: Any) -> str | None:
    """Allow the account controls to render after a fresh navigation."""
    deadline = time.monotonic() + 5
    while True:
        error = facebook_session_error(page)
        if error not in {"marketplace_session_unverified", "marketplace_access_unverified"}:
            return error
        if time.monotonic() >= deadline:
            return error
        page.wait_for_timeout(100)


class FacebookSessionStore:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.state_path = home / "facebook-session.json"
        self.metadata_path = home / "facebook-session-evidence.json"

    def _write(self, path: Path, data: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def save_verified(
        self,
        state: dict,
        config: str,
        verified_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        # Caller must have verified the live authenticated Marketplace page.
        now = verified_at or datetime.now(timezone.utc)
        self._write(self.state_path, state)
        self._write(
            self.metadata_path,
            {
                "schema_version": 1,
                "status": "verified",
                "verified_at": now.isoformat(),
                "expires_at": (
                    expires_at or now + timedelta(seconds=SESSION_TTL_SECONDS)
                ).isoformat(),
                "state_digest": hashlib.sha256(self.state_path.read_bytes()).hexdigest(),
                "account_fingerprint": account_fingerprint(config),
            },
        )

    def snapshot(self, config: str, require_fresh: bool = True) -> str | None:
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            raw = self.state_path.read_bytes()
            if (
                metadata.get("status") != "verified"
                or metadata.get("account_fingerprint") != account_fingerprint(config)
                or hashlib.sha256(raw).hexdigest() != metadata.get("state_digest")
            ):
                return None
            if require_fresh and datetime.fromisoformat(metadata["expires_at"]) <= datetime.now(
                timezone.utc
            ):
                return None
            return raw.decode("utf-8")
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self, config: str) -> dict:
        credentials = tomllib.loads(config).get("marketplace", {}).get("facebook", {})
        configured = bool(credentials.get("username") and credentials.get("password"))
        valid = self.snapshot(config) is not None
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8")) if valid else {}
        except (OSError, ValueError):
            metadata, valid = {}, False
        return {
            "status": (
                "verified"
                if valid
                else ("expired" if self.state_path.exists() else "login_required")
            ),
            "session_valid": valid,
            "credentials_configured": configured,
            "username_hint": (
                (str(credentials.get("username", ""))[:2] + "***")
                if credentials.get("username")
                else None
            ),
            "verified_at": metadata.get("verified_at"),
            "expires_at": metadata.get("expires_at"),
            "blocked_reason": (
                None
                if valid
                else (
                    "marketplace_session_unverified"
                    if configured or self.state_path.exists()
                    else "marketplace_credentials_missing"
                )
            ),
        }

    def clear(self) -> None:
        self.state_path.unlink(missing_ok=True)
        self.metadata_path.unlink(missing_ok=True)

    def invalidate_snapshot(self, state: str, config: str) -> None:
        if self.snapshot(config, require_fresh=False) == state:
            self.metadata_path.unlink(missing_ok=True)
