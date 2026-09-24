from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.hanggent_integration import (
    HanggentIntegrationService,
    HanggentWatch,
)
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app


def test_namespaced_watch_round_trip_preserves_credentials(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[marketplace.facebook]\nusername = "owner@example.com"\npassword = "secret"\n\n'
        '[user.me]\npushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n',
        encoding="utf-8",
    )
    integration = HanggentIntegrationService(ConfigFileService([config_file]))

    integration.upsert(
        "hanggent_u7_w12",
        HanggentWatch(
            search_phrases=["Tesla Model Y", "Tesla Model 3"],
            search_region="can",
            search_interval="30m",
            max_price=50000,
        ),
    )

    content = config_file.read_text(encoding="utf-8")
    assert 'password = "secret"' in content
    assert "[item.hanggent_u7_w12]" in content
    assert 'search_phrases = ["Tesla Model Y", "Tesla Model 3"]' in content
    assert 'search_region = "can"' in content
    assert "max_price = 50000" in content

    integration.delete("hanggent_u7_w12")
    content = config_file.read_text(encoding="utf-8")
    assert "[item.hanggent_u7_w12]" in content
    assert "enabled = false" in content
    assert 'password = "secret"' in content


def test_rejects_non_hanggent_section_name(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\n", encoding="utf-8")
    integration = HanggentIntegrationService(ConfigFileService([config_file]))

    try:
        integration.delete("facebook")
    except ValueError as exc:
        assert "Invalid Hanggent watch key" in str(exc)
    else:
        raise AssertionError("invalid integration key was accepted")


def test_internal_api_requires_shared_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[marketplace.facebook]\nusername = "owner@example.com"\npassword = "secret"\n\n'
        '[item.default]\nsearch_phrases = ["vehicle"]\nenabled = false\n\n'
        '[user.me]\npushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AIMM_INTEGRATION_TOKEN", "shared-secret")
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        AuthState(),
        ConfigFileService([config_file]),
        handler,
    )
    client = TestClient(app)
    payload = {
        "search_phrases": ["Tesla Model Y"],
        "search_region": "can",
        "search_interval": "60m",
        "enabled": True,
    }

    assert client.put("/api/integration/watches/hanggent_u7_w12", json=payload).status_code == 401
    response = client.put(
        "/api/integration/watches/hanggent_u7_w12",
        json=payload,
        headers={"Authorization": "Bearer shared-secret"},
    )
    assert response.status_code == 200
    assert response.json()["watch_key"] == "hanggent_u7_w12"
