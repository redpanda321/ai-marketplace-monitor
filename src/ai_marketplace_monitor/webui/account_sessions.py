"""Tenant-owned interactive login; never exposes the shared desktop/VNC."""

from __future__ import annotations

import base64
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from ..facebook_session import (
    MARKETPLACE_URL,
    FacebookSessionStore,
    account_fingerprint,
    facebook_session_error,
    verify_facebook_session,
)

LOGIN_LIFETIME_SECONDS = 600


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
        self, user_id: int, home: Path, read_config: Callable[[], str], control_lock: Any
    ) -> None:
        self.user_id = user_id
        self.id = uuid4().hex
        self.store = FacebookSessionStore(home)
        self.read_config = read_config
        self.control_lock = control_lock
        self.config = read_config()
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
        }

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
                "blocked_reason": "marketplace_browser_unavailable"
                if self.state == "browser_unavailable"
                else "marketplace_login_session_expired",
            }
        if not self.ready.is_set():
            return self.public()
        future = Future()
        self.commands.put_nowait((action, future))
        return future.result(timeout=28)

    def inspect(self, page: Any) -> dict[str, Any]:
        error = facebook_session_error(page)
        self.state = error.removeprefix("marketplace_") if error else "ready_to_verify"
        url = urlsplit(page.url)
        return {
            **self.public(),
            "blocked_reason": error,
            "page_url": f"{url.scheme}://{url.netloc}{url.path}",
            "screenshot": base64.b64encode(page.screenshot(type="png")).decode("ascii"),
            "viewport_width": 1280,
            "viewport_height": 800,
        }

    def run(self) -> None:
        try:
            import json

            from playwright.sync_api import sync_playwright

            state = self.store.snapshot(self.config, require_fresh=False)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    storage_state=json.loads(state) if state else None,
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
                        kind = action.get("action", "inspect")
                        if kind == "verify":
                            page.goto(
                                MARKETPLACE_URL, wait_until="domcontentloaded", timeout=15000
                            )
                            error = verify_facebook_session(page)
                            if error:
                                future.set_result(self.inspect(page))
                                continue
                            with self.control_lock:
                                if self.closed.is_set() or account_fingerprint(
                                    self.config
                                ) != account_fingerprint(self.read_config()):
                                    raise ValueError("marketplace_account_changed")
                                self.store.save_verified(context.storage_state(), self.config)
                                self.state = "verified"
                                self.closed.set()
                            future.set_result(
                                {
                                    "user_id": self.user_id,
                                    **self.store.status(self.read_config()),
                                    "browser_active": False,
                                }
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
                        future.set_result(self.inspect(page))
                    except Exception:
                        future.set_exception(ValueError("marketplace_login_action_failed"))
                browser.close()
        except Exception:
            self.state = "browser_unavailable"
        finally:
            if self.state not in ("verified", "browser_unavailable"):
                self.state = "expired"
            self.config = ""
            self.closed.set()
            self.ready.set()
            while not self.commands.empty():
                _, future = self.commands.get_nowait()
                if not future.done():
                    future.set_exception(ValueError("marketplace_login_session_expired"))


class AccountSessions:
    def __init__(self, root: Path, read_config: Callable[[int], str]) -> None:
        self.root = root
        self.read_config = read_config
        self.lock = threading.Lock()
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
