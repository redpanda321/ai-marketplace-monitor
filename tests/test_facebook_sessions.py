import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_marketplace_monitor.facebook_session import (
    FacebookSessionStore,
    facebook_session_error,
    verify_facebook_session,
)
from ai_marketplace_monitor.webui.account_sessions import AccountSessions, login_request_allowed
from ai_marketplace_monitor.webui.hanggent_integration import (
    HanggentIntegrationService,
    HanggentWatch,
)
from ai_marketplace_monitor.webui.run_receipts import RunReceipts

CONFIG = '[marketplace.facebook]\nusername="qa-only"\npassword="qa-only-secret"\n'
STATE = {
    "cookies": [{"name": "c_user", "value": "test-only", "domain": ".facebook.com", "path": "/"}],
    "origins": [],
}


def test_session_is_bound_to_account_home_digest_and_expiry(tmp_path: Path) -> None:
    store = FacebookSessionStore(tmp_path / "u8")
    store.save_verified(STATE, CONFIG)
    assert store.status(CONFIG)["session_valid"] is True
    assert FacebookSessionStore(tmp_path / "u9").snapshot(CONFIG) is None
    assert store.snapshot(CONFIG.replace("qa-only-secret", "changed-secret")) is None
    metadata = json.loads(store.metadata_path.read_text())
    metadata["expires_at"] = "2000-01-01T00:00:00+00:00"
    store.metadata_path.write_text(json.dumps(metadata))
    assert store.status(CONFIG)["session_valid"] is False
    store.save_verified(STATE, CONFIG)
    store.state_path.write_text("{}")
    assert store.snapshot(CONFIG) is None


def test_real_account_control_label_and_missing_evidence_are_handled() -> None:
    # Ground truth observed in the user's live Chrome Marketplace search:
    # Account, your profile; Search Marketplace; one main region.
    def page(
        account: bool = True, cookie: bool = True, main: bool = True, path: str = "/marketplace/"
    ):
        def locator(selector: str):
            if "input[" in selector:
                count = 0
            elif "Account, your profile" in selector:
                count = int(account)
            elif "navigation" in selector:
                count = 1
            else:
                count = int(main)
            return SimpleNamespace(count=lambda: count)

        return SimpleNamespace(
            url="https://www.facebook.com" + path,
            locator=locator,
            context=SimpleNamespace(cookies=lambda *args: STATE["cookies"] if cookie else []),
        )

    assert facebook_session_error(page()) is None
    assert facebook_session_error(page(account=False)) == "marketplace_session_unverified"
    assert facebook_session_error(page(cookie=False)) == "marketplace_session_unverified"
    assert facebook_session_error(page(main=False)) == "marketplace_access_unverified"
    assert facebook_session_error(page(path="/checkpoint/")) == "marketplace_challenge_required"


def test_verified_manual_session_without_saved_password_is_queued_and_frozen(
    tmp_path: Path,
) -> None:
    home = tmp_path / "u8"
    config = '[marketplace.facebook]\nsearch_city="vancouver"\n'
    store = FacebookSessionStore(home)
    store.save_verified(STATE, config)
    runs = RunReceipts(tmp_path, start_worker=False)
    receipt = runs.enqueue("hanggent_u8_w6", 27, config)
    assert receipt["status"] == "queued"
    store.clear()
    with runs.connect() as db:
        snapshot = db.execute("SELECT session_state FROM jobs WHERE run=27").fetchone()[0]
        assert json.loads(snapshot) == STATE
    assert "test-only" not in json.dumps(runs.read("hanggent_u8_w6", 27))


def test_connect_preserves_watch_ai_config_and_disconnect_cancels_queued_snapshot(
    tmp_path: Path,
) -> None:
    service = HanggentIntegrationService(tmp_path, start_worker=False)
    service._write(8, CONFIG + '\n[ai.openai]\napi_key="qa-ai-preserved"\n')
    service.upsert("hanggent_u8_w6", HanggentWatch(["Tesla Model Y"], "can"))
    service.connect_account(8, {"search_city": "vancouver"})
    text = service._read(8)
    assert "qa-ai-preserved" in text and "[item.hanggent_u8_w6]" in text
    store = FacebookSessionStore(tmp_path / "u8")
    store.save_verified(STATE, text)
    service.enqueue_run("hanggent_u8_w6", 27)
    service.disconnect_account(8)
    assert service.accounts.status(8)["session_valid"] is False
    assert "qa-only-secret" not in service._read(8)
    assert "qa-ai-preserved" in service._read(8)
    assert service.run_status("hanggent_u8_w6", 27)["status"] == "paused_needs_attention"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.facebook.com/login/", True),
        ("https://static.xx.fbcdn.net/file", True),
        ("http://www.facebook.com/", False),
        ("https://facebook.com.evil.invalid/", False),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("file:///etc/passwd", False),
    ],
)
def test_private_login_cannot_navigate_to_arbitrary_network_targets(
    url: str, expected: bool
) -> None:
    assert login_request_allowed(url) is expected


def test_login_session_id_from_another_user_cannot_control_a_browser(tmp_path: Path) -> None:
    sessions = AccountSessions(tmp_path, lambda user: "")
    sessions.sessions[8] = SimpleNamespace(id="owned-by-eight")
    with pytest.raises(ValueError, match="not_found"):
        sessions.perform(9, "owned-by-eight", {"action": "inspect"})


def test_fresh_navigation_waits_for_all_ui_evidence_and_stops_on_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_marketplace_monitor import facebook_session

    checks = iter(["marketplace_session_unverified", "marketplace_access_unverified", None])
    monkeypatch.setattr(facebook_session, "facebook_session_error", lambda page: next(checks))
    waits = []
    assert verify_facebook_session(SimpleNamespace(wait_for_timeout=waits.append)) is None
    assert waits == [100, 100]
    monkeypatch.setattr(
        facebook_session, "facebook_session_error", lambda page: "marketplace_challenge_required"
    )
    assert verify_facebook_session(SimpleNamespace()) == "marketplace_challenge_required"


def test_legacy_job_database_is_upgraded_without_rewriting_old_receipt(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "run-receipts.sqlite3") as db:
        db.execute(
            "CREATE TABLE jobs (watch TEXT, run INTEGER, user INTEGER, status TEXT, deadline REAL, config TEXT, receipt TEXT, results TEXT, PRIMARY KEY(watch,run))"
        )
        old = json.dumps({"status": "failed", "run_id": 19, "error_code": "prior-failure"})
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
            ("hanggent_u8_w6", 19, 8, "failed", None, "", old, None),
        )
    runs = RunReceipts(tmp_path, start_worker=False)
    assert runs.read("hanggent_u8_w6", 19) == json.loads(old)
    with runs.connect() as db:
        assert db.execute("SELECT session_state FROM jobs").fetchone() == (None,)


def test_disconnecting_running_scan_is_rejected_without_changing_its_identity(
    tmp_path: Path,
) -> None:
    service = HanggentIntegrationService(tmp_path, start_worker=False)
    service._write(8, CONFIG)
    service.upsert("hanggent_u8_w6", HanggentWatch(["Tesla Model Y"], "can"))
    service.enqueue_run("hanggent_u8_w6", 27)
    service.receipts.claim()
    with pytest.raises(ValueError, match="marketplace_scan_in_progress"):
        service.disconnect_account(8)
    assert "qa-only-secret" in service._read(8)
    assert service.run_status("hanggent_u8_w6", 27)["status"] == "running"
