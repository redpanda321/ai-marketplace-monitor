from pathlib import Path

from fastapi.testclient import TestClient

from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app


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
