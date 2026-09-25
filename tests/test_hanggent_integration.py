from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.hanggent_integration import (
    HanggentIntegrationService,
    HanggentWatch,
)
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app


def test_namespaced_watch_round_trip_preserves_credentials(tmp_path: Path) -> None:
    integration = HanggentIntegrationService(tmp_path)
    integration.update_settings(
        7,
        {
            "facebook_username": "owner@example.com",
            "facebook_password": "secret",
            "search_city": "burnaby",
            "ai_provider": "openai",
            "ai_api_key": "sk-test",
            "ai_model": "gpt-4o",
        },
    )

    integration.upsert(
        "hanggent_u7_w12",
        HanggentWatch(
            search_phrases=["Tesla Model Y", "Tesla Model 3"],
            search_region="can",
            search_interval="30m",
            max_price=50000,
        ),
    )

    config_file = tmp_path / "u7" / "config.toml"
    content = config_file.read_text(encoding="utf-8")
    assert 'password = "secret"' in content
    assert "[item.hanggent_u7_w12]" in content
    assert 'search_phrases = ["Tesla Model Y", "Tesla Model 3"]' in content
    assert 'search_region = "can"' in content
    assert "max_price = 50000" in content
    parsed = Config([config_file])
    assert parsed.marketplace["facebook"].username == "owner@example.com"
    assert parsed.item["hanggent_u7_w12"].search_region == ["can"]
    assert parsed.ai["openai"].model == "gpt-4o"

    integration.delete("hanggent_u7_w12")
    content = config_file.read_text(encoding="utf-8")
    assert "[item.hanggent_u7_w12]" not in content
    assert 'password = "secret"' in content


def test_rejects_non_hanggent_section_name(tmp_path: Path) -> None:
    integration = HanggentIntegrationService(tmp_path)

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
    monkeypatch.setenv("AIMM_TENANT_ROOT", str(tmp_path / "users"))
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

    settings = client.put(
        "/api/integration/users/7/settings",
        json={
            "facebook_username": "owner@example.com",
            "facebook_password": "secret",
            "search_city": "burnaby",
        },
        headers={"Authorization": "Bearer shared-secret"},
    )
    assert settings.status_code == 200
    tenant_config = (tmp_path / "users" / "u7" / "config.toml").read_text(encoding="utf-8")
    assert 'username = "owner@example.com"' in tenant_config
    assert "[item.hanggent_u7_w12]" in tenant_config
