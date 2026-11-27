"""Browser diagnostics use fake Playwright operations and no network or real account."""

import builtins
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import playwright.sync_api
import pytest

from ai_marketplace_monitor.webui import account_sessions

CONFIG = '[marketplace.facebook]\nusername="saved-user-731"\npassword="saved-password-842"\n'
STORAGE = {
    "cookies": [{"name": "c_user", "value": "cookie-private-953"}],
    "origins": [
        {
            "origin": "https://www.facebook.com",
            "localStorage": [{"name": "token", "value": "local-private-164"}],
        }
    ],
}


class BrowserHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        self.monkeypatch = monkeypatch
        self.home = home
        self.errors: dict[str, Exception] = {}
        self.threads: list[threading.Thread] = []
        self.close_calls = 0
        self.saved = False
        self.verification_error = None
        self.snapshot = json.dumps(STORAGE)
        self.page = SimpleNamespace(
            url="https://www.facebook.com/login/?private=query-private-275#fragment-private-386",
            goto=lambda *args, **kwargs: self.operation("navigation"),
            screenshot=lambda **kwargs: self.operation("inspection") or b"fake-png",
            keyboard=SimpleNamespace(
                insert_text=lambda value: self.operation("interaction"),
                press=lambda key: self.operation("interaction"),
            ),
            mouse=SimpleNamespace(
                click=lambda *args: self.operation("interaction"),
                wheel=lambda *args: self.operation("interaction"),
            ),
        )
        self.context = SimpleNamespace(
            route=lambda *args: None,
            route_web_socket=lambda *args: None,
            set_default_timeout=lambda *args: None,
            new_page=lambda: self.page,
            storage_state=lambda: STORAGE,
        )

        def new_context(**kwargs: Any) -> SimpleNamespace:
            self.operation("context")
            return self.context

        self.browser = SimpleNamespace(new_context=new_context, close=self.close)

        def launch(**kwargs: Any) -> SimpleNamespace:
            self.operation("launch")
            return self.browser

        class Manager:
            def __enter__(inner_self) -> SimpleNamespace:
                return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

            def __exit__(inner_self, *args: Any) -> None:
                self.operation("manager_shutdown")

        def thread_factory(**kwargs: Any) -> threading.Thread:
            thread = threading.Thread(**kwargs)
            self.threads.append(thread)
            return thread

        monkeypatch.setattr(playwright.sync_api, "sync_playwright", Manager)
        monkeypatch.setattr(
            account_sessions,
            "threading",
            SimpleNamespace(Event=threading.Event, Thread=thread_factory, Lock=threading.Lock),
        )
        monkeypatch.setattr(
            account_sessions.FacebookSessionStore,
            "snapshot",
            lambda *args, **kwargs: self.restore(),
        )
        monkeypatch.setattr(account_sessions.FacebookSessionStore, "save_verified", self.save)
        monkeypatch.setattr(
            account_sessions.FacebookSessionStore,
            "status",
            lambda *args: {"status": "verified" if self.saved else "login_required"},
        )
        monkeypatch.setattr(
            account_sessions, "facebook_session_error", lambda page: "marketplace_login_required"
        )

        def verify(page: Any) -> str | None:
            self.operation("verification")
            return self.verification_error

        monkeypatch.setattr(account_sessions, "verify_facebook_session", verify)

    def operation(self, stage: str) -> None:
        error = self.errors.pop(stage, None)
        if error:
            raise error

    def restore(self) -> str | None:
        self.operation("session_restore")
        return self.snapshot

    def save(self, *args: Any) -> None:
        self.operation("session_save")
        self.saved = True

    def close(self) -> None:
        self.close_calls += 1
        self.operation("shutdown")

    def start(self) -> account_sessions.AccountBrowser:
        self.session = account_sessions.AccountBrowser(8, self.home, lambda: CONFIG, threading.Lock())
        assert self.session.ready.wait(3), "Worker did not signal readiness"
        return self.session

    def finish(self) -> None:
        if hasattr(self, "session"):
            self.session.closed.set()
        for thread in self.threads:
            thread.join(timeout=3)
            assert not thread.is_alive(), "Worker did not shut down"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    result = BrowserHarness(monkeypatch, tmp_path / "u8")
    try:
        yield result
    finally:
        result.finish()


def assert_failure(result: dict, stage: str) -> dict:
    failure = result["failure"]
    assert set(failure) == {"code", "stage", "reference"}
    assert failure["code"] == f"marketplace_browser_{stage}_failed"
    assert failure["stage"] == stage
    assert re.fullmatch(r"[0-9a-f]{32}", failure["reference"])
    return failure


@pytest.mark.parametrize("stage", ["session_restore", "launch", "context", "navigation"])
def test_startup_failure_keeps_safe_reason_and_internal_trace(
    harness: BrowserHarness, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    caplog.set_level(logging.ERROR, logger=account_sessions.__name__)
    harness.errors[stage] = TimeoutError("diagnostic-operation-marker")
    session = harness.start()
    harness.finish()
    result = session.call({"action": "inspect"})
    assert result["status"] == "browser_unavailable"
    assert result["blocked_reason"] == "marketplace_browser_unavailable"
    assert result["browser_active"] is False
    failure = assert_failure(result, stage)
    assert "diagnostic-operation-marker" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "Traceback" in caplog.text
    assert failure["reference"] in caplog.text
    assert session.id in caplog.text
    assert "user_id=8" in caplog.text
    assert "diagnostic-operation-marker" not in json.dumps(result)
    assert all(record.exc_info is None for record in caplog.records)
    assert session.ready.is_set() and session.closed.is_set()
    assert session.config == ""
    assert harness.close_calls == int(stage in {"context", "navigation"})


def test_missing_dependency_is_classified_at_import(
    harness: BrowserHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = builtins.__import__

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright.sync_api":
            raise ModuleNotFoundError("fixture dependency missing")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    session = harness.start()
    assert_failure(session.public(), "dependency_load")


def test_malformed_snapshot_is_restore_failure(harness: BrowserHarness) -> None:
    harness.snapshot = "{invalid json"
    session = harness.start()
    assert_failure(session.public(), "session_restore")
    assert harness.close_calls == 0


@pytest.mark.parametrize("action_stage", ["interaction", "verification", "session_save", "inspection"])
def test_action_failure_is_nonterminal_and_successful_retry_clears_it(
    harness: BrowserHarness, action_stage: str
) -> None:
    session = harness.start()
    harness.errors[action_stage] = RuntimeError("fixture-action-marker")
    action = (
        {"action": "type", "text": "typed-private-497"}
        if action_stage == "interaction"
        else {"action": "inspect" if action_stage == "inspection" else "verify"}
    )
    with pytest.raises(ValueError, match="^marketplace_login_action_failed$") as caught:
        session.call(action)
    assert caught.value.__cause__ is not None
    assert "fixture-action-marker" in str(caught.value.__cause__)
    assert_failure(session.public(), action_stage)
    assert session.closed.is_set() is False
    retry = session.call({"action": "inspect"})
    assert retry["status"] == "login_required"
    assert retry.get("failure") is None
    assert session.public().get("failure") is None


def test_verify_navigation_timeout_is_navigation_failure(harness: BrowserHarness) -> None:
    session = harness.start()
    harness.errors["navigation"] = TimeoutError("fixture verification navigation timed out")
    with pytest.raises(ValueError, match="marketplace_login_action_failed"):
        session.call({"action": "verify"})
    assert_failure(session.public(), "navigation")


def test_rejected_verification_preserves_login_reason_without_runtime_failure(
    harness: BrowserHarness,
) -> None:
    session = harness.start()
    harness.verification_error = "marketplace_login_required"
    result = session.call({"action": "verify"})
    assert result["blocked_reason"] == "marketplace_login_required"
    assert result.get("failure") is None
    assert not harness.saved


def test_all_diagnostic_channels_redact_credentials_storage_actions_and_urls(
    harness: BrowserHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger=account_sessions.__name__)
    session = harness.start()
    typed = "typed-private-497"
    session.call({"action": "type", "text": typed})
    secrets = ["saved-user-731", "saved-password-842", "cookie-private-953", "local-private-164", typed]
    message = "launch-engine-marker " + " ".join(secrets) + " " + harness.page.url
    try:
        raise OSError("root-cause-marker " + message)
    except OSError as cause:
        try:
            raise RuntimeError("wrapper-marker " + message) from cause
        except RuntimeError as error:
            harness.errors["interaction"] = error
    with pytest.raises(ValueError) as caught:
        session.call({"action": "key", "key": "Enter"})
    logged = caplog.text
    cause_text = str(caught.value.__cause__)
    public = json.dumps(session.public())
    for secret in [*secrets, "query-private-275", "fragment-private-386"]:
        assert secret not in logged
        assert secret not in cause_text
        assert secret not in public
    assert "launch-engine-marker" in logged
    assert "wrapper-marker" in logged and "root-cause-marker" in logged
    assert "OSError" in logged and "RuntimeError" in logged
    assert "direct cause" in logged and "Traceback" in logged
    assert "https://www.facebook.com/login/" in logged
    assert_failure(session.public(), "interaction")


def test_shutdown_does_not_replace_original_failure(
    harness: BrowserHarness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.errors["navigation"] = TimeoutError("first-cause-marker")
    harness.errors["shutdown"] = RuntimeError("close-cause-marker")
    harness.errors["manager_shutdown"] = RuntimeError("manager-cause-marker")
    session = harness.start()
    harness.finish()
    assert_failure(session.public(), "navigation")
    assert harness.close_calls == 1
    for marker in ["first-cause-marker", "close-cause-marker", "manager-cause-marker"]:
        assert marker in caplog.text


def test_shutdown_failure_is_classified_and_releases_pending_commands(harness: BrowserHarness) -> None:
    session = harness.start()
    harness.errors["shutdown"] = RuntimeError("fixture-close-error")
    harness.finish()
    assert_failure(session.public(), "shutdown")
    assert session.closed.is_set() and session.ready.is_set()
    assert session.state == "browser_unavailable"


def test_account_status_includes_failure_for_closed_browser(harness: BrowserHarness) -> None:
    harness.errors["launch"] = RuntimeError("fixture launch error")
    session = harness.start()
    sessions = account_sessions.AccountSessions(harness.home.parent, lambda user: CONFIG)
    sessions.sessions[8] = session
    result = sessions.status(8)
    assert_failure(result, "launch")
    assert result["status"] == "browser_unavailable"
    assert result["blocked_reason"] == "marketplace_browser_unavailable"
