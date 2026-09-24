import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app


def _jwt(payload: dict, secret: str) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = encode({"alg": "HS256", "typ": "JWT"})
    body = encode(payload)
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def test_index_renders_reverse_proxy_base_path(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    config = WebUIConfig(
        config_files=[config_file],
        log_handler=handler,
        base_path="/marketplace-monitor/",
    )
    app = create_app(config, AuthState(), ConfigFileService([config_file]), handler)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert 'window.__AIMM_BASE_PATH__ = "/marketplace-monitor"' in response.text
    assert 'src="/marketplace-monitor/static/app.js"' in response.text
    assert "__AIMM_PUBLIC_BASE__" not in response.text


def test_index_uses_root_paths_without_prefix(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    config = WebUIConfig(config_files=[config_file], log_handler=handler)
    app = create_app(config, AuthState(), ConfigFileService([config_file]), handler)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert 'window.__AIMM_BASE_PATH__ = ""' in response.text
    assert 'src="/static/app.js"' in response.text


def test_hanggent_session_exchange_authenticates_webui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    config = WebUIConfig(config_files=[config_file], log_handler=handler)
    state = AuthState()
    state.exposed = True
    monkeypatch.setenv("AIMM_HANGGENT_JWT_SECRET", "shared-secret")
    app = create_app(config, state, ConfigFileService([config_file]), handler)
    client = TestClient(app)
    token = _jwt({"id": 42, "exp": time.time() + 60}, "shared-secret")

    exchanged = client.post("/api/hanggent/session", headers={"Authorization": f"Bearer {token}"})

    assert exchanged.status_code == 200
    assert client.cookies.get("aimm_session")
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["can_manage_config"] is False
    assert "config_files" not in status.json()
    assert client.get("/api/config/files").status_code == 403
    assert client.get("/api/config/file/primary").status_code == 403


def test_hanggent_session_exchange_rejects_invalid_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    state = AuthState()
    state.exposed = True
    monkeypatch.setenv("AIMM_HANGGENT_JWT_SECRET", "shared-secret")
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        state,
        ConfigFileService([config_file]),
        handler,
    )
    token = _jwt({"id": 42, "exp": time.time() - 1}, "shared-secret")

    response = TestClient(app).post(
        "/api/hanggent/session", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
