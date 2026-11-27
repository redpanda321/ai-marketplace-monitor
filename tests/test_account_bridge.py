"""Fake-cookie contracts. These do not assert real Facebook authentication."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from ai_marketplace_monitor.facebook_session import FacebookSessionStore
from ai_marketplace_monitor.webui import account_bridge as bridge
from ai_marketplace_monitor.webui.account_bridge_api import register_bridge_routes
from ai_marketplace_monitor.webui.hanggent_integration import HanggentIntegrationService


def cookies() -> list[dict]:
    return [
        {
            "name": name,
            "value": value,
            "domain": ".facebook.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
            "expires": -1,
        }
        for name, value in (("c_user", "80000001"), ("xs", "fake-session-only"))
    ]


@pytest.fixture
def service(tmp_path: Path) -> HanggentIntegrationService:
    return HanggentIntegrationService(tmp_path, start_worker=False)


def upload(
    service: HanggentIntegrationService,
    ticket: dict,
    operation: str | None = None,
    payload: list[dict] | None = None,
) -> dict:
    return service.bridges.complete(
        ticket["bridge_id"], ticket["capability"], operation or str(uuid4()), payload or cookies()
    )


def wait(service: HanggentIntegrationService) -> None:
    for worker in list(service.bridges.workers.values()):
        worker.join(5)
        assert not worker.is_alive()


def owner_status(service: HanggentIntegrationService, ticket: dict) -> dict:
    return service.bridges.status(ticket["bridge_id"], owner=ticket["user_id"])


def test_capability_is_hashed_scoped_and_absolute_expiry(
    service: HanggentIntegrationService,
) -> None:
    ticket = service.bridges.begin(8)
    assert ticket["user_id"] == 8 and len(ticket["capability"]) == 43
    assert ticket["capability"].encode() not in service.bridges.path.read_bytes()
    for options in (
        {"owner": 9},
        {"capability": "x" * 43},
        {"capability": "header.payload.signature"},
    ):
        with pytest.raises(bridge.BridgeError, match="bridge_not_found"):
            service.bridges.status(ticket["bridge_id"], **options)
    with service.bridges.connect() as db:
        db.execute(
            "UPDATE bridges SET status='blocked',deadline=? WHERE id=?",
            (time.time() - 1, ticket["bridge_id"]),
        )
    with pytest.raises(bridge.BridgeError, match="expired"):
        service.bridges.status(ticket["bridge_id"], capability=ticket["capability"])
    assert owner_status(service, ticket)["status"] == "blocked"


def test_verified_once_identical_concurrent_retry_and_changed_replay(
    service: HanggentIntegrationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls = []

    def verify(state: dict, identity: str) -> tuple[dict, None]:
        calls.append(identity)
        entered.set()
        assert release.wait(5)
        return state, None

    monkeypatch.setattr(bridge, "live_verify", verify)
    ticket = service.bridges.begin(8)
    operation = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: upload(service, ticket, operation), range(6)))
    assert entered.wait(2)
    assert all(result["status"] == "verifying" for result in results)
    changed = cookies()
    changed[1]["value"] = "different-fake-secret"
    with pytest.raises(bridge.BridgeError, match="upload_conflict"):
        upload(service, ticket, operation, changed)
    with pytest.raises(bridge.BridgeError, match="upload_conflict"):
        upload(service, ticket)
    assert not FacebookSessionStore(service._user_home(8)).state_path.exists()
    release.set()
    wait(service)
    assert owner_status(service, ticket)["session_valid"] is True
    before = FacebookSessionStore(service._user_home(8)).metadata_path.read_bytes()
    assert upload(service, ticket, operation)["status"] == "verified"
    assert FacebookSessionStore(service._user_home(8)).metadata_path.read_bytes() == before
    assert calls == ["80000001"]
    assert b"fake-session-only" not in service.bridges.path.read_bytes()
    # A later different verified session is not this bridge's saved receipt.
    FacebookSessionStore(service._user_home(8)).save_verified(
        {"cookies": [], "origins": []}, service._read(8)
    )
    assert owner_status(service, ticket)["session_valid"] is False


@pytest.mark.parametrize(
    "error", ["challenge_required", "login_required", "access_unverified", "navigation_failed"]
)
def test_live_failure_preserves_previous_verified_store(
    service: HanggentIntegrationService, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    store = FacebookSessionStore(service._user_home(8))
    previous = {"cookies": [{"name": "c_user", "value": "previous"}], "origins": []}
    store.save_verified(previous, "")
    original = store.snapshot("")
    monkeypatch.setattr(bridge, "live_verify", lambda state, identity: (None, error))
    ticket = service.bridges.begin(8)
    upload(service, ticket)
    wait(service)
    result = owner_status(service, ticket)
    assert result["status"] == "blocked" and result["error_code"] == error
    assert result["session_valid"] is False
    assert store.snapshot("") == original


@pytest.mark.parametrize(
    "mutation", ["cancel", "config", "credentials", "disconnect", "private", "new", "expiry"]
)
def test_account_mutations_during_live_validation_prevent_save(
    service: HanggentIntegrationService, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    entered, release = threading.Event(), threading.Event()

    def verify(state: dict, identity: str) -> tuple[dict, None]:
        entered.set()
        assert release.wait(5)
        return state, None

    monkeypatch.setattr(bridge, "live_verify", verify)
    ticket = service.bridges.begin(8)
    upload(service, ticket)
    assert entered.wait(2)
    if mutation == "cancel":
        service.bridges.cancel(ticket["bridge_id"], 8)
    elif mutation == "config":
        service.connect_account(8, {"search_city": "burnaby"})
    elif mutation == "credentials":
        service.update_settings(
            8, {"facebook_username": "fake-user", "facebook_password": "fake-password"}
        )
    elif mutation == "disconnect":
        service.disconnect_account(8)
    elif mutation == "private":
        monkeypatch.setattr(service.accounts, "begin", lambda owner: {"user_id": owner})
        service.begin_account_login(8)
    elif mutation == "new":
        service.bridges.begin(8)
    else:
        with service.bridges.connect() as db:
            db.execute(
                "UPDATE bridges SET deadline=? WHERE id=?", (time.time() - 1, ticket["bridge_id"])
            )
    release.set()
    wait(service)
    assert owner_status(service, ticket)["status"] in ("cancelled", "expired")
    assert not FacebookSessionStore(service._user_home(8)).state_path.exists()


def test_cancelled_but_running_verifiers_still_consume_capacity(
    service: HanggentIntegrationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    monkeypatch.setattr(
        bridge, "live_verify", lambda state, identity: (release.wait(5) and state, None)
    )
    try:
        for owner in (8, 9):
            ticket = service.bridges.begin(owner)
            upload(service, ticket)
            service.bridges.cancel(ticket["bridge_id"], owner)
        third = service.bridges.begin(10)
        with pytest.raises(bridge.BridgeError, match="capacity_busy"):
            upload(service, third)
        assert owner_status(service, third)["status"] == "pending"
    finally:
        release.set()
        wait(service)


@pytest.mark.parametrize("changed", [False, True])
def test_restart_invalidates_uncertain_matching_save_preserves_later_state(
    service: HanggentIntegrationService, changed: bool
) -> None:
    ticket = service.bridges.begin(8)
    state, _ = bridge.parse_cookies(cookies())
    store = FacebookSessionStore(service._user_home(8))
    store.save_verified(state, "")
    with service.bridges.connect() as db:
        db.execute(
            "UPDATE bridges SET status='saving',state_digest=? WHERE id=?",
            (bridge.state_digest(state), ticket["bridge_id"]),
        )
    if changed:
        store.save_verified({"cookies": [], "origins": []}, "")
    restarted = HanggentIntegrationService(service._root, start_worker=False)
    assert owner_status(restarted, ticket)["status"] == "failed"
    assert bool(store.snapshot("")) is changed


def test_restart_does_not_resume_verifying_upload(service: HanggentIntegrationService) -> None:
    ticket = service.bridges.begin(8)
    with service.bridges.connect() as db:
        db.execute("UPDATE bridges SET status='verifying' WHERE id=?", (ticket["bridge_id"],))
    restarted = HanggentIntegrationService(service._root, start_worker=False)
    assert owner_status(restarted, ticket)["error_code"] == "verification_interrupted"
    assert not restarted.bridges.workers


@pytest.mark.parametrize(
    "update",
    [
        {"domain": ".facebook.com.attacker.test"},
        {"name": "unknown"},
        {"path": "/settings"},
        {"partitionKey": {"topLevelSite": "https://facebook.com"}},
        {"value": "x\nsecret"},
        {"value": "x" * 4097},
        {"secure": "true"},
        {"httpOnly": 1},
        {"expires": float("nan")},
        {"expires": 0},
        {"sameSite": "unknown"},
        {"value": "not-digits"},
    ],
)
def test_invalid_cookie_schema_fails_closed(update: dict[str, Any]) -> None:
    payload = cookies()
    payload[0].update(update)
    with pytest.raises(bridge.BridgeError, match="invalid_cookies"):
        bridge.parse_cookies(payload)


def test_duplicates_and_missing_required_auth_rejected() -> None:
    with pytest.raises(bridge.BridgeError):
        bridge.parse_cookies([cookies()[0], cookies()[0]])
    with pytest.raises(bridge.BridgeError):
        bridge.parse_cookies(cookies()[:1])


def test_running_scan_refuses_begin_and_save_without_mutating_account(
    service: HanggentIntegrationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = service.bridges.begin(8)
    with service.receipts.connect() as db:
        db.execute(
            "INSERT INTO jobs(watch,run,user,status) VALUES ('hanggent_u8_w6',1,8,'running')"
        )
    with pytest.raises(ValueError, match="marketplace_scan_in_progress"):
        service.bridges.begin(8)
    assert owner_status(service, ticket)["status"] == "pending"
    monkeypatch.setattr(bridge, "live_verify", lambda state, identity: (state, None))
    upload(service, ticket)
    wait(service)
    assert owner_status(service, ticket)["session_valid"] is False
    assert not FacebookSessionStore(service._user_home(8)).state_path.exists()


def test_api_requires_integration_auth_owner_and_no_cookie_echo(
    service: HanggentIntegrationService,
) -> None:
    app = FastAPI()

    def require(request: Request) -> None:
        if request.headers.get("Authorization") != "Bearer internal-fake":
            raise HTTPException(401)

    register_bridge_routes(app, service, require)
    client = TestClient(app)
    base = "/api/integration/users/8/account/bridge"
    assert client.post(base).status_code == 401
    headers = {"Authorization": "Bearer internal-fake"}
    ticket = client.post(base, headers=headers).json()
    assert (
        client.get(
            base.replace("users/8", "users/9") + "/" + ticket["bridge_id"], headers=headers
        ).status_code
        == 409
    )
    public = "/api/integration/account/bridge/" + ticket["bridge_id"]
    assert (
        client.post(
            public + "/preview", headers=headers, json={"capability": ticket["capability"]}
        ).json()["user_id"]
        == 8
    )
    denied = client.post(
        public + "/complete",
        headers=headers,
        json={
            "capability": ticket["capability"],
            "operation_id": str(uuid4()),
            "cookies": cookies(),
            "user_id": 9,
        },
    )
    assert denied.status_code == 422
    assert "fake-session-only" not in denied.text and ticket["capability"] not in denied.text
    assert (
        client.post(
            public + "/complete", headers=headers, content='{"secret":"' + "x" * 32768 + '"}'
        ).status_code
        == 413
    )
