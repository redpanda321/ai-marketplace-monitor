import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import playwright.sync_api
import pytest

from ai_marketplace_monitor.webui.account_sessions import AccountBrowser


@pytest.mark.parametrize("forms,stored_credentials", [(2, True), (1, True), (2, False)])
def test_manual_login_remains_interactive_with_duplicate_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forms: int,
    stored_credentials: bool,
) -> None:
    original = playwright.sync_api.sync_playwright
    observations = []
    errors = []
    finished = threading.Event()
    html = (
        "<style>body{margin:0}input{display:block;height:40px;width:250px}</style>"
        + '<form><input name="email"><input name="pass" type="password"></form>' * forms
        + '<script>window.submissions=0;document.addEventListener("submit", e => '
        "{e.preventDefault();window.submissions++})</script>"
    )

    class LocalPlaywright:
        def __enter__(self) -> SimpleNamespace:
            self.manager = original()
            real = self.manager.__enter__()

            def launch(**kwargs):
                browser = real.chromium.launch(**kwargs)
                original_context = browser.new_context

                def new_context(**context_kwargs):
                    context = original_context(**context_kwargs)
                    original_page = context.new_page

                    def new_page():
                        page = original_page()
                        # Page routes take precedence over the production context route.
                        page.route(
                            "**/*",
                            lambda route: route.fulfill(body=html, content_type="text/html"),
                        )
                        screenshot = page.screenshot

                        def capture(**capture_kwargs):
                            observations.append(page.evaluate("""() => ({
                                values: [...document.querySelectorAll('input')].map(x => x.value),
                                submissions: window.submissions
                            })"""))
                            return screenshot(**capture_kwargs)

                        page.screenshot = capture
                        locator = page.locator

                        def observe_locator(selector: str):
                            result = locator(selector)
                            fill = result.fill

                            def observe_fill(value: str, **fill_kwargs):
                                try:
                                    return fill(value, **fill_kwargs)
                                except Exception as error:
                                    errors.append(str(error))
                                    raise

                            result.fill = observe_fill
                            return result

                        page.locator = observe_locator
                        return page

                    context.new_page = new_page
                    return context

                browser.new_context = new_context
                return browser

            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def __exit__(self, *args) -> Any:
            try:
                return self.manager.__exit__(*args)
            finally:
                finished.set()

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", LocalPlaywright)
    config = '[marketplace.facebook]\nsearch_city="vancouver"\n'
    if stored_credentials:
        config += 'username="fixture-saved-user"\npassword="fixture-saved-secret"\n'
    browser = AccountBrowser(8, tmp_path / "u8", lambda: config, threading.Lock())
    try:
        assert browser.ready.wait(15), "Browser did not become ready"
        result = browser.call({"action": "inspect"})
        assert result["browser_active"] is True, {"result": result, "errors": errors}
        assert result["status"] == "login_required"
        assert result["blocked_reason"] == "marketplace_login_required"
        assert observations[-1] == {"values": [""] * (forms * 2), "submissions": 0}
        assert browser.read_config() == config

        browser.call({"action": "click", "x": 50, "y": 20})
        browser.call({"action": "type", "text": "fixture-manual-user"})
        browser.call({"action": "key", "key": "Tab"})
        result = browser.call({"action": "type", "text": "fixture-manual-secret"})
        assert result["browser_active"] is True
        assert observations[-1]["values"][:2] == ["fixture-manual-user", "fixture-manual-secret"]
        assert observations[-1]["submissions"] == 0

        result = browser.call({"action": "verify"})
        assert result["session_valid"] is False
        assert result["browser_active"] is True
        assert not browser.store.state_path.exists()
    finally:
        browser.closed.set()
        assert finished.wait(10), "Browser worker did not close"
