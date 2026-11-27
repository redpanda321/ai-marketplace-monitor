import pytest

from ai_marketplace_monitor.webui.account_sessions import login_request_allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/recaptcha/enterprise.js?render=explicit",
        "https://www.gstatic.com/recaptcha/releases/test/recaptcha__en.js",
        "https://www.google.com/recaptcha/enterprise/anchor?ar=1",
        "https://recaptcha.google.com/recaptcha/enterprise/bframe",
        "https://www.google.com/recaptcha/enterprise/reload?k=test-key",
        "https://www.google.com:443/recaptcha/enterprise.js",
    ],
)
def test_private_login_loads_recaptcha_scripts_frames_and_connections(url: str) -> None:
    assert login_request_allowed(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://www.google.com/recaptcha/enterprise.js",
        "http://www.gstatic.com/recaptcha/releases/test/recaptcha__en.js",
        "http://recaptcha.google.com/recaptcha/enterprise/anchor",
        "https://www.google.com/",
        "https://www.google.com/search?q=recaptcha",
        "https://www.gstatic.com/unrelated.js",
        "https://recaptcha.google.com/unrelated",
        "https://www.google.com/recaptcha-lookalike/enterprise.js",
        "https://www.google.com/recaptcha",
        "https://google.com/recaptcha/enterprise.js",
        "https://accounts.google.com/recaptcha/enterprise.js",
        "https://nested.www.google.com/recaptcha/enterprise.js",
        "https://www.google.com.evil.invalid/recaptcha/enterprise.js",
        "https://www.gstatic.com.evil.invalid/recaptcha/enterprise.js",
        "https://recaptcha.google.com.evil.invalid/recaptcha/enterprise/anchor",
        "https://www.google.com@evil.invalid/recaptcha/enterprise.js",
        "https://www.google.com:8443/recaptcha/enterprise.js",
    ],
)
def test_recaptcha_exception_rejects_unrelated_network_targets(url: str) -> None:
    assert login_request_allowed(url) is False
