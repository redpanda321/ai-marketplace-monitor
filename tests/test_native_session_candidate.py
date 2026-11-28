import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_marketplace_monitor import facebook, tenant_worker
from ai_marketplace_monitor.facebook_session import FacebookSessionStore
from ai_marketplace_monitor.webui.run_receipts import RunReceipts

CONFIG = '[marketplace.facebook]\nusername="fixture"\npassword="fixture-secret"\n'
STATE = {
    "cookies": [
        {"name": "c_user", "value": "fixture-account", "domain": ".facebook.com", "path": "/"}
    ],
    "origins": [],
}


@pytest.mark.parametrize("challenge", [False, True])
def test_native_search_captures_only_after_live_marketplace_verification(
    monkeypatch: pytest.MonkeyPatch, challenge: bool
) -> None:
    captures = []
    context = SimpleNamespace(
        cookies=lambda *args: STATE["cookies"],
        storage_state=lambda: captures.append(True) or STATE,
    )
    page = SimpleNamespace(
        context=context,
        locator=lambda selector: SimpleNamespace(count=lambda: 0 if "input[" in selector else 1),
    )
    marketplace = facebook.FacebookMarketplace.__new__(facebook.FacebookMarketplace)
    marketplace.page, marketplace.logger = page, None
    marketplace.require_verified_session = True
    marketplace.translator = lambda text: text
    marketplace.goto_url = lambda url: setattr(
        page, "url", "https://www.facebook.com/checkpoint/" if challenge else url
    )
    fields = dict.fromkeys(
        [
            "condition",
            "date_listed",
            "delivery_method",
            "availability",
            "sort_by",
            "city_name",
            "radius",
            "currency",
            "min_price",
            "max_price",
            "category",
        ]
    )
    fields.update(
        search_city=["vancouver"],
        city_name=["Vancouver"],
        searched_count=0,
        search_phrases=["Tesla"],
        name="fixture",
        marketplace="facebook",
    )
    marketplace.config = SimpleNamespace(**fields)
    monkeypatch.setattr(facebook.FacebookSearchResultPage, "get_listings", lambda self: [])
    monkeypatch.setattr(facebook.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(facebook.counter, "increment", lambda *args: None)
    if challenge:
        with pytest.raises(PermissionError):
            list(marketplace.search(SimpleNamespace(**fields)))
        assert captures == []
        assert not hasattr(marketplace, "verified_session_candidate")
    else:
        assert list(marketplace.search(SimpleNamespace(**fields))) == []
        assert captures == [True]
        assert marketplace.verified_session_candidate["storage_state"] == STATE
        assert marketplace.session_verified is True


def test_scoped_worker_writes_private_candidate_separate_from_public_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = {"schema_version": 1, "verified_at": time.time(), "storage_state": STATE}
    monitor = SimpleNamespace(
        run_item_once=lambda *args, **kwargs: [],
        stop_monitor=lambda: None,
        verified_session_candidate=candidate,
    )
    monkeypatch.setattr(tenant_worker, "MarketplaceMonitor", lambda *args: monitor)
    output, private = tmp_path / "output.json", tmp_path / "verified-session.json"
    monkeypatch.setattr(
        tenant_worker.sys, "argv", ["worker", "hanggent_u7_w12", "config.toml", str(output)]
    )
    monkeypatch.setenv("AIMM_VERIFIED_SESSION_CANDIDATE", str(private))
    assert tenant_worker.main() == 0
    assert json.loads(private.read_text()) == candidate
    assert json.loads(output.read_text()) == {"session_valid": True, "results": []}


@pytest.mark.parametrize(
    "mode",
    [
        "cancelled",
        "expired_lease",
        "account_changed",
        "future",
        "expired",
        "missing_cookie",
        "expired_cookie",
        "write_failed",
    ],
)
def test_invalid_or_stale_native_completion_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = tmp_path / "u7"
    home.mkdir()
    runs = RunReceipts(
        tmp_path,
        start_worker=False,
        read_config=lambda user: (
            CONFIG.replace("fixture-secret", "changed") if mode == "account_changed" else CONFIG
        ),
    )
    runs.enqueue("hanggent_u7_w12", 19, CONFIG)

    def process(args: list[str], **kwargs: Any) -> Any:
        state = json.loads(json.dumps(STATE))
        verified_at = time.time()
        if mode == "future":
            verified_at += 60
        if mode == "expired":
            verified_at -= 90000
        if mode == "missing_cookie":
            state["cookies"] = []
        if mode == "expired_cookie":
            state["cookies"][0]["expires"] = time.time() - 60
        output = Path(args[-1])
        output.with_name("verified-session.json").write_text(
            json.dumps({"schema_version": 1, "verified_at": verified_at, "storage_state": state})
        )
        output.write_text(json.dumps({"session_valid": True, "results": []}))
        if mode in ("cancelled", "expired_lease"):
            with runs.connect() as db:
                db.execute(
                    "UPDATE jobs SET status='failed'"
                    if mode == "cancelled"
                    else "UPDATE jobs SET deadline=0"
                )
        return SimpleNamespace(wait=lambda **kwargs: 0)

    if mode == "write_failed":
        monkeypatch.setattr(
            FacebookSessionStore,
            "save_verified",
            lambda *args: (_ for _ in ()).throw(OSError("fixture failure")),
        )
    monkeypatch.setattr(subprocess, "Popen", process)
    runs.execute(*runs.claim())
    assert FacebookSessionStore(home).snapshot(CONFIG) is None
    assert not (home / "facebook-session.json").exists()
    receipt = runs.read("hanggent_u7_w12", 19, True)
    assert receipt["status"] != "completed"
    assert "results_digest" not in receipt
    assert "fixture-account" not in json.dumps(receipt)
