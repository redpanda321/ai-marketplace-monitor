"""Tenant-owned interactive login; never exposes the shared desktop/VNC."""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import time
import traceback
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote, quote_plus, urlsplit
from uuid import uuid4

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from ..facebook_session import (
    MARKETPLACE_URL,
    FacebookSessionStore,
    account_fingerprint,
    facebook_session_error,
    verify_facebook_session,
)

LOGIN_LIFETIME_SECONDS = 600
logger = logging.getLogger(__name__)
FailureStage = Literal[
    "dependency_load",
    "session_restore",
    "launch",
    "context",
    "navigation",
    "verification",
    "session_save",
    "interaction",
    "inspection",
    "shutdown",
]
URL_IN_DIAGNOSTIC = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>]+")


@dataclass(frozen=True)
class BrowserFailure:
    stage: FailureStage
    reference: str

    def public(self) -> dict[str, str]:
        return {
            "code": f"marketplace_browser_{self.stage}_failed",
            "stage": self.stage,
            "reference": self.reference,
        }


def login_request_allowed(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme == "https"
        and host in ("www.google.com", "www.gstatic.com", "recaptcha.google.com")
        and parsed.netloc.lower() in (host, f"{host}:443")
        and parsed.path.startswith("/recaptcha/")
    ):
        return True
    return parsed.scheme == "https" and any(
        host == domain or host.endswith("." + domain)
        for domain in (
            "facebook.com",
            "facebook.net",
            "fbcdn.net",
            "fbsbx.com",
            "arkoselabs.com",
            "funcaptcha.com",
            "hcaptcha.com",
        )
    )


class AccountBrowser:
    def __init__(
        self, user_id: int, home: Path, read_config: Callable[[], str], control_lock: Any,
        publish_guard: Callable[[], Any] = nullcontext,
    ) -> None:
        self.user_id = user_id
        self.id = uuid4().hex
        self.store = FacebookSessionStore(home)
        self.read_config = read_config
        self.control_lock = control_lock
        self.publish_guard = publish_guard
        self.config = read_config()
        self.failure: BrowserFailure | None = None
        self._secrets: set[str] = set()
        self._remember_secret(self.config)
        self.commands = queue.Queue(maxsize=8)
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.deadline = time.monotonic() + LOGIN_LIFETIME_SECONDS
        self.state = "opening"
        threading.Thread(target=self.run, daemon=True, name=f"facebook-login-{user_id}").start()

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "session_id": self.id,
            "status": self.state,
            "session_valid": False,
            "browser_active": not self.closed.is_set(),
            **({"failure": self.failure.public()} if self.failure else {}),
        }

    def _remember_secret(self, value: Any) -> None:
        if isinstance(value, str) and value:
            self._secrets.update(
                (
                    value,
                    repr(value)[1:-1],
                    json.dumps(value)[1:-1],
                    quote(value, safe=""),
                    quote_plus(value),
                )
            )

    def _remember_storage(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "value":
                    self._remember_secret(item)
                else:
                    self._remember_storage(item)
        elif isinstance(value, list):
            for item in value:
                self._remember_storage(item)

    def _safe_trace(self, error: Exception) -> str:
        secrets = sorted(self._secrets, key=len, reverse=True)
        secret_pattern = (
            re.compile("|".join(re.escape(secret) for secret in secrets)) if secrets else None
        )
        url_secrets = [secret for secret in secrets if "://" in secret]
        url_secret_pattern = (
            re.compile("|".join(re.escape(secret) for secret in url_secrets))
            if url_secrets
            else None
        )

        def redact_url(match: re.Match[str]) -> str:
            try:
                url = urlsplit(match.group())
                return f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}{url.path}"
            except ValueError:
                return "[redacted-url]"

        def sanitize(message: str) -> str:
            if url_secret_pattern:
                message = url_secret_pattern.sub("[redacted]", message)
            message = URL_IN_DIAGNOSTIC.sub(redact_url, message)
            return secret_pattern.sub("[redacted]", message) if secret_pattern else message

        def render(summary: traceback.TracebackException) -> str:
            parts = []
            if summary.__cause__:
                parts.extend(
                    (
                        render(summary.__cause__),
                        "\nThe above exception was the direct cause of the following exception:\n\n",
                    )
                )
            elif summary.__context__ and not summary.__suppress_context__:
                parts.extend(
                    (
                        render(summary.__context__),
                        "\nDuring handling of the above exception, another exception occurred:\n\n",
                    )
                )
            if summary.stack:
                parts.append("Traceback (most recent call last):\n")
                for frame in summary.stack:
                    filename = URL_IN_DIAGNOSTIC.sub(redact_url, frame.filename)
                    parts.append(f'  File "{filename}", line {frame.lineno}, in {frame.name}\n')
            exception_type = summary.exc_type
            name = exception_type.__qualname__
            if exception_type.__module__ not in {"builtins", "__main__"}:
                name = f"{exception_type.__module__}.{name}"
            exception_only = "".join(summary.format_exception_only())
            marker = re.search(rf"(?m)^{re.escape(name)}(?::|$)", exception_only)
            message = exception_only[marker.start() + len(name) :] if marker else "\n"
            parts.append(name + sanitize(message))
            for child in getattr(summary, "exceptions", None) or ():
                parts.append(render(child))
            return "".join(parts)

        return render(traceback.TracebackException.from_exception(error, lookup_lines=False))

    def _record_failure(
        self, error: Exception, stage: FailureStage, preserve: bool = False
    ) -> RuntimeError:
        failure = BrowserFailure(stage, uuid4().hex)
        if not preserve:
            self.failure = failure
        diagnostic = self._safe_trace(error)
        exception_type = f"{type(error).__module__}.{type(error).__qualname__}"
        logger.error(
            "Marketplace login browser failure user_id=%s session_id=%s reference=%s stage=%s code=%s exception_type=%s\n%s",
            self.user_id,
            self.id,
            failure.reference,
            failure.stage,
            failure.public()["code"],
            exception_type,
            diagnostic,
            extra={
                "user_id": self.user_id,
                "session_id": self.id,
                "exception_type": exception_type,
                **failure.public(),
            },
        )
        return RuntimeError(diagnostic)

    def call(self, action: dict) -> dict[str, Any]:
        if self.closed.is_set():
            if self.state == "verified":
                return {
                    "user_id": self.user_id,
                    **self.store.status(self.read_config()),
                    "browser_active": False,
                }
            return {
                **self.public(),
                "blocked_reason": (
                    "marketplace_browser_unavailable"
                    if self.state == "browser_unavailable"
                    else "marketplace_login_session_expired"
                ),
            }
        if not self.ready.is_set():
            return self.public()
        future = Future()
        self.commands.put_nowait((action, future))
        return future.result(timeout=28)

    def inspect(self, page: Any) -> dict[str, Any]:
        error = facebook_session_error(page)
        url = urlsplit(page.url)
        screenshot = base64.b64encode(page.screenshot(type="png")).decode("ascii")
        self.state = error.removeprefix("marketplace_") if error else "ready_to_verify"
        self.failure = None
        return {
            **self.public(),
            "blocked_reason": error,
            "page_url": f"{url.scheme}://{url.netloc}{url.path}",
            "screenshot": screenshot,
            "viewport_width": 1280,
            "viewport_height": 800,
        }

    def run(self) -> None:
        stage: FailureStage = "session_restore"
        try:
            credentials = tomllib.loads(self.config).get("marketplace", {}).get("facebook", {})
            for name in ("username", "password"):
                self._remember_secret(credentials.get(name))
            stage = "dependency_load"
            from playwright.sync_api import sync_playwright

            stage = "session_restore"
            state = self.store.snapshot(self.config, require_fresh=False)
            self._remember_secret(state)
            storage_state = json.loads(state) if state else None
            if storage_state is not None:
                self._remember_secret(str(storage_state))
            self._remember_storage(storage_state)
            stage = "dependency_load"
            with sync_playwright() as playwright:
                browser = None
                try:
                    stage = "launch"
                    browser = playwright.chromium.launch(headless=True)
                    stage = "context"
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                        storage_state=storage_state,
                    )
                    context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if login_request_allowed(route.request.url)
                            else route.abort()
                        ),
                    )
                    context.route_web_socket("**/*", lambda websocket: websocket.close())
                    context.set_default_timeout(5000)
                    page = context.new_page()
                    stage = "navigation"
                    page.goto(MARKETPLACE_URL, wait_until="domcontentloaded", timeout=30000)
                    self.state = "login_required"
                    self.ready.set()
                    while not self.closed.is_set() and time.monotonic() < self.deadline:
                        try:
                            action, future = self.commands.get(timeout=1)
                        except queue.Empty:
                            continue
                        try:
                            if future.cancelled():
                                continue
                            stage = "interaction"
                            kind = action.get("action", "inspect")
                            if kind == "verify":
                                stage = "navigation"
                                page.goto(
                                    MARKETPLACE_URL, wait_until="domcontentloaded", timeout=15000
                                )
                                stage = "verification"
                                error = verify_facebook_session(page)
                                if error:
                                    stage = "inspection"
                                    future.set_result(self.inspect(page))
                                    continue
                                with self.control_lock, self.publish_guard():
                                    if self.closed.is_set() or account_fingerprint(
                                        self.config
                                    ) != account_fingerprint(self.read_config()):
                                        raise ValueError("marketplace_account_changed")
                                    stage = "session_save"
                                    verified_state = context.storage_state()
                                    self._remember_storage(verified_state)
                                    self.store.save_verified(verified_state, self.config)
                                    result = self.store.status(self.read_config())
                                    self.state = "verified"
                                    self.failure = None
                                    self.closed.set()
                                future.set_result(
                                    {"user_id": self.user_id, **result, "browser_active": False}
                                )
                                break
                            if kind == "click":
                                x, y = action.get("x"), action.get("y")
                                if (
                                    not isinstance(x, (int, float))
                                    or not isinstance(y, (int, float))
                                    or not 0 <= x < 1280
                                    or not 0 <= y < 800
                                ):
                                    raise ValueError("Invalid login click coordinates")
                                page.mouse.click(x, y)
                            elif kind == "type":
                                value = action.get("text")
                                self._remember_secret(value)
                                if not isinstance(value, str) or len(value) > 1024:
                                    raise ValueError("Invalid login input")
                                page.keyboard.insert_text(value)
                            elif kind == "key":
                                key = action.get("key")
                                if key not in {
                                    "Enter",
                                    "Tab",
                                    "Backspace",
                                    "Escape",
                                    "ArrowLeft",
                                    "ArrowRight",
                                    "ArrowUp",
                                    "ArrowDown",
                                }:
                                    raise ValueError("Invalid login key")
                                page.keyboard.press(key)
                            elif kind == "scroll":
                                amount = action.get("delta")
                                if amount not in (-500, 500):
                                    raise ValueError("Invalid login scroll")
                                page.mouse.wheel(0, amount)
                            elif kind != "inspect":
                                raise ValueError("Invalid login action")
                            stage = "inspection"
                            future.set_result(self.inspect(page))
                        except Exception as error:
                            cause = self._record_failure(error, stage)
                            failure = ValueError("marketplace_login_action_failed")
                            failure.__cause__ = cause
                            future.set_exception(failure)
                except Exception as error:
                    self._record_failure(error, stage)
                    self.state = "browser_unavailable"
                finally:
                    stage = "shutdown"
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception as error:
                            self._record_failure(error, stage, preserve=self.failure is not None)
                            if self.state != "verified":
                                self.state = "browser_unavailable"
        except Exception as error:
            self._record_failure(error, stage, preserve=self.failure is not None)
            if self.state != "verified":
                self.state = "browser_unavailable"
        finally:
            if self.state not in ("verified", "browser_unavailable"):
                self.state = "expired"
            self.config = ""
            self._secrets.clear()
            self.closed.set()
            self.ready.set()
            while not self.commands.empty():
                _, future = self.commands.get_nowait()
                if not future.done():
                    future.set_exception(ValueError("marketplace_login_session_expired"))


class AccountSessions:
    def __init__(self, root: Path, read_config: Callable[[int], str], *, control_lock: Any = None,
                 publish_guard: Callable[[int], Any] = lambda owner: nullcontext()) -> None:
        self.root = root
        self.read_config = read_config
        self.lock = control_lock if control_lock is not None else threading.Lock()
        self.publish_guard = publish_guard
        self.sessions = {}

    def status(self, user_id: int) -> dict[str, Any]:
        result = {
            "user_id": user_id,
            **FacebookSessionStore(self.root / f"u{user_id}").status(self.read_config(user_id)),
        }
        with self.lock:
            session = self.sessions.get(user_id)
            if session and not session.closed.is_set():
                result.update(
                    browser_active=True, session_id=session.id, browser_status=session.state
                )
            elif session and session.state == "browser_unavailable":
                result.update(
                    status="browser_unavailable",
                    blocked_reason="marketplace_browser_unavailable",
                    browser_active=False,
                )
            if session and session.failure:
                result["failure"] = session.failure.public()
        return result

    def begin(self, user_id: int) -> dict[str, Any]:
        with self.lock:
            current = self.sessions.get(user_id)
            if current is None or current.closed.is_set():
                if sum(not item.closed.is_set() for item in self.sessions.values()) >= 2:
                    raise ValueError("marketplace_login_capacity_busy")
                current = AccountBrowser(
                    user_id,
                    self.root / f"u{user_id}",
                    lambda: self.read_config(user_id),
                    self.lock,
                    lambda: self.publish_guard(user_id),
                )
                self.sessions[user_id] = current
            return current.public()

    def perform(self, user_id: int, session_id: str, action: dict) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(user_id)
        if session is None or session.id != session_id:
            raise ValueError("marketplace_login_session_not_found")
        return session.call(action)

    def close(self, user_id: int) -> None:
        with self.lock:
            session = self.sessions.pop(user_id, None)
            if session:
                session.closed.set()
