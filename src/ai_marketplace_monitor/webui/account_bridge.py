from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from ..facebook_session import (
    MARKETPLACE_URL,
    FacebookSessionStore,
    account_fingerprint,
    verify_facebook_session,
)
from .account_sessions import login_request_allowed

DESTINATION = "https://staging.hangent.com"
LIFETIME_SECONDS = 300
MAX_BODY = 32768
COOKIE_NAMES = frozenset({"c_user", "xs", "datr", "sb", "fr", "wd", "presence"})
ACTIVE = ("pending", "verifying", "saving")


class BridgeError(ValueError):
    """Only fixed public codes cross the transport boundary."""


def state_digest(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def parse_cookies(cookies: Any) -> tuple[dict, str]:
    if not isinstance(cookies, list) or not 2 <= len(cookies) <= len(COOKIE_NAMES):
        raise BridgeError("invalid_cookies")
    approved = []
    names = set()
    for cookie in cookies:
        if not isinstance(cookie, dict) or set(cookie) != {
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "httpOnly",
            "sameSite",
            "expires",
        }:
            raise BridgeError("invalid_cookies")
        name, value, domain, path = (cookie[key] for key in ("name", "value", "domain", "path"))
        if (
            not isinstance(name, str)
            or name not in COOKIE_NAMES
            or name in names
            or not isinstance(value, str)
            or not 1 <= len(value) <= 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or domain not in (".facebook.com", "facebook.com", "www.facebook.com")
            or not isinstance(path, str)
            or path not in ("/", "/marketplace", "/marketplace/")
            or type(cookie["secure"]) is not bool
            or type(cookie["httpOnly"]) is not bool
            or cookie["sameSite"] not in ("Strict", "Lax", "None")
            or type(cookie["expires"]) not in (int, float)
            or not math.isfinite(cookie["expires"])
            or (
                cookie["expires"] != -1
                and not time.time() < cookie["expires"] < time.time() + 400 * 86400
            )
            or (cookie["sameSite"] == "None" and not cookie["secure"])
        ):
            raise BridgeError("invalid_cookies")
        names.add(name)
        approved.append(dict(cookie))
    if not {"c_user", "xs"} <= names:
        raise BridgeError("invalid_cookies")
    identity = next(cookie["value"] for cookie in approved if cookie["name"] == "c_user")
    if not identity.isascii() or not identity.isdigit() or len(identity) > 32:
        raise BridgeError("invalid_cookies")
    return {
        "cookies": sorted(approved, key=lambda cookie: cookie["name"]),
        "origins": [],
    }, identity


def live_verify(state: dict, identity: str) -> tuple[dict | None, str | None]:
    from playwright.sync_api import sync_playwright

    stage = "launch"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=10000)
            try:
                return _verify_context(browser, state, identity)
            finally:
                stage = "shutdown"
                browser.close()
    except Exception:
        return None, f"{stage}_failed"


def _verify_context(browser: Any, state: dict, identity: str) -> tuple[dict | None, str | None]:
    stage = "context"
    try:
        context = browser.new_context(locale="en-US", storage_state=state)
        context.route(
            "**/*",
            lambda route: (
                route.continue_() if login_request_allowed(route.request.url) else route.abort()
            ),
        )
        context.route_web_socket("**/*", lambda socket: socket.close())
        context.set_default_timeout(5000)
        page = context.new_page()
        stage = "navigation"
        page.goto(MARKETPLACE_URL, wait_until="domcontentloaded", timeout=20000)
        stage = "verification"
        error = verify_facebook_session(page)
        if error:
            return None, error.removeprefix("marketplace_")
        observed = [
            cookie["value"]
            for cookie in context.cookies(MARKETPLACE_URL)
            if cookie["name"] == "c_user"
        ]
        if observed != [identity]:
            return None, "account_mismatch"
        return {"cookies": context.cookies(), "origins": []}, None
    except Exception:
        return None, f"{stage}_failed"


class AccountBridges:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.path = service._root / "account-bridges.sqlite3"
        self.workers: dict[str, threading.Thread] = {}
        self.slots = threading.BoundedSemaphore(2)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS generations (owner INTEGER PRIMARY KEY, generation INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bridges ("
                "id TEXT PRIMARY KEY, owner INTEGER NOT NULL, capability_hash TEXT NOT NULL, "
                "deadline REAL NOT NULL, generation INTEGER NOT NULL, fingerprint TEXT NOT NULL, "
                "status TEXT NOT NULL, operation TEXT, payload_digest TEXT, state_digest TEXT, "
                "error_code TEXT)"
            )
        self.path.chmod(0o600)
        self.recover()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        with self.service._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT * FROM bridges WHERE status IN ('verifying','saving')"
            ).fetchall():
                if row["status"] == "saving":
                    self.invalidate_uncertain_save(row)
                db.execute(
                    "UPDATE bridges SET status='failed',error_code='verification_interrupted' WHERE id=?",
                    (row["id"],),
                )
            db.execute(
                "DELETE FROM bridges WHERE deadline < ? AND status NOT IN ('verifying','saving')",
                (time.time() - 86400,),
            )

    def cancel_owner(self, owner: int) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO generations VALUES (?,1) ON CONFLICT(owner) DO UPDATE SET generation=generation+1",
                (owner,),
            )
            db.execute(
                "UPDATE bridges SET status='cancelled',error_code='account_changed' WHERE owner=? AND status IN ('pending','verifying')",
                (owner,),
            )

    def begin(self, owner: int) -> dict:
        with self.service._lock, self.service.receipts.account_update(owner):
            self.cancel_owner(owner)
            self.service.accounts.close(owner)
            capability, bridge_id = secrets.token_urlsafe(32), str(uuid4())
            with self.connect() as db:
                generation = db.execute(
                    "SELECT generation FROM generations WHERE owner=?", (owner,)
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO bridges (id,owner,capability_hash,deadline,generation,fingerprint,status) VALUES (?,?,?,?,?,?,'pending')",
                    (
                        bridge_id,
                        owner,
                        hashlib.sha256(capability.encode()).hexdigest(),
                        time.time() + LIFETIME_SECONDS,
                        generation,
                        account_fingerprint(self.service._read(owner)),
                    ),
                )
                row = db.execute("SELECT * FROM bridges WHERE id=?", (bridge_id,)).fetchone()
            return {**self.public(row), "capability": capability}

    def lookup(
        self,
        db: sqlite3.Connection,
        bridge_id: str,
        capability: str | None = None,
        owner: int | None = None,
    ) -> sqlite3.Row:
        row = db.execute("SELECT * FROM bridges WHERE id=?", (bridge_id,)).fetchone()
        if (
            row is None
            or (owner is not None and row["owner"] != owner)
            or (
                capability is not None
                and not hmac.compare_digest(
                    row["capability_hash"], hashlib.sha256(capability.encode()).hexdigest()
                )
            )
        ):
            raise BridgeError("bridge_not_found")
        if capability is not None and row["deadline"] <= time.time():
            raise BridgeError("expired")
        if row["deadline"] <= time.time() and row["status"] in ACTIVE:
            db.execute(
                "UPDATE bridges SET status='expired',error_code='expired' WHERE id=?", (bridge_id,)
            )
            row = db.execute("SELECT * FROM bridges WHERE id=?", (bridge_id,)).fetchone()
        return row

    def public(self, row: sqlite3.Row) -> dict:
        status = row["status"]
        valid = False
        if status == "verified":
            store = FacebookSessionStore(self.service._user_home(row["owner"]))
            snapshot = store.snapshot(self.service._read(row["owner"]))
            valid = bool(
                snapshot and hashlib.sha256(snapshot.encode()).hexdigest() == row["state_digest"]
            )
            if not valid:
                status = "expired"
        return {
            "bridge_id": row["id"],
            "user_id": row["owner"],
            "destination": DESTINATION,
            "deadline": datetime.fromtimestamp(row["deadline"], timezone.utc).isoformat(),
            "status": "verifying" if status == "saving" else status,
            "session_valid": valid,
            "error_code": row["error_code"] if status != "expired" else "expired",
        }

    def status(
        self, bridge_id: str, *, capability: str | None = None, owner: int | None = None
    ) -> dict:
        with self.service._lock, self.connect() as db:
            return self.public(self.lookup(db, bridge_id, capability, owner))

    def cancel(self, bridge_id: str, owner: int) -> dict:
        with self.service._lock, self.connect() as db:
            self.lookup(db, bridge_id, owner=owner)
            db.execute(
                "UPDATE bridges SET status='cancelled',error_code='cancelled' WHERE id=? AND status IN ('pending','verifying')",
                (bridge_id,),
            )
            return self.public(self.lookup(db, bridge_id, owner=owner))

    def complete(self, bridge_id: str, capability: str, operation: str, cookies: Any) -> dict:
        try:
            if str(UUID(operation)) != operation:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("invalid_operation") from None
        state, identity = parse_cookies(cookies)
        digest = state_digest(state)
        with self.service._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.lookup(db, bridge_id, capability)
            if row["operation"] is not None:
                if row["operation"] != operation or row["payload_digest"] != digest:
                    raise BridgeError("upload_conflict")
                return self.public(row)
            if row["status"] != "pending":
                return self.public(row)
            if not self.slots.acquire(blocking=False):
                raise BridgeError("capacity_busy")
            try:
                db.execute(
                    "UPDATE bridges SET status='verifying',operation=?,payload_digest=? WHERE id=? AND status='pending'",
                    (operation, digest, bridge_id),
                )
                db.commit()
            except Exception:
                self.slots.release()
                raise
        worker = threading.Thread(
            target=self.verify,
            args=(bridge_id, state, identity),
            daemon=True,
            name="facebook-bridge",
        )
        self.workers[bridge_id] = worker
        try:
            worker.start()
        except Exception:
            self.slots.release()
            self.workers.pop(bridge_id, None)
            self.finish_failure(bridge_id, "failed", "verification_failed")
        return self.status(bridge_id, capability=capability)

    def verify(self, bridge_id: str, state: dict, identity: str) -> None:
        try:
            verified, error = live_verify(state, identity)
            if error or verified is None:
                self.finish_failure(bridge_id, "blocked", error or "session_unverified")
                return
            with self.service._lock:
                with self.connect() as db:
                    row = self.lookup(db, bridge_id)
                    owner = row["owner"]
                with self.service.receipts.account_update(owner, cancel_pending=True):
                    with self.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = self.lookup(db, bridge_id)
                        generation = db.execute(
                            "SELECT generation FROM generations WHERE owner=?", (owner,)
                        ).fetchone()[0]
                        config = self.service._read(owner)
                        if row["status"] != "verifying":
                            return
                        if (
                            generation != row["generation"]
                            or account_fingerprint(config) != row["fingerprint"]
                        ):
                            db.execute(
                                "UPDATE bridges SET status='cancelled',error_code='account_changed' WHERE id=?",
                                (bridge_id,),
                            )
                            return
                        db.execute(
                            "UPDATE bridges SET status='saving',state_digest=? WHERE id=?",
                            (state_digest(verified), bridge_id),
                        )
                    store = FacebookSessionStore(self.service._user_home(owner))
                    store.save_verified(verified, config)
                    if store.snapshot(config) is None:
                        raise BridgeError("save_unverified")
                    with self.connect() as db:
                        db.execute(
                            "UPDATE bridges SET status='verified',error_code=NULL WHERE id=? AND status='saving'",
                            (bridge_id,),
                        )
        except Exception:
            self.finish_failure(bridge_id, "failed", "save_failed")
        finally:
            state.clear()
            self.workers.pop(bridge_id, None)
            self.slots.release()

    def finish_failure(self, bridge_id: str, status: str, error: str) -> None:
        with self.service._lock, self.connect() as db:
            row = db.execute("SELECT * FROM bridges WHERE id=?", (bridge_id,)).fetchone()
            if row["status"] == "saving":
                self.invalidate_uncertain_save(row)
            db.execute(
                "UPDATE bridges SET status=?,error_code=? WHERE id=? AND status IN ('verifying','saving')",
                (status, error, bridge_id),
            )

    def invalidate_uncertain_save(self, row: sqlite3.Row) -> None:
        store = FacebookSessionStore(self.service._user_home(row["owner"]))
        try:
            if hashlib.sha256(store.state_path.read_bytes()).hexdigest() == row["state_digest"]:
                store.metadata_path.unlink(missing_ok=True)
        except OSError:
            pass
