import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ai_marketplace_monitor.webui.run_receipts import RunReceipts, digest

CONFIG = '[marketplace.facebook]\nusername="qa"\npassword="private-password"\n'


def store(tmp_path: Path) -> RunReceipts:
    (tmp_path / "u7").mkdir(exist_ok=True)
    return RunReceipts(tmp_path, start_worker=False)


def test_concurrent_retry_and_restart_preserve_one_receipt(tmp_path: Path) -> None:
    runs = store(tmp_path)
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda _: runs.enqueue("hanggent_u7_w12", 19, CONFIG), range(8)))
    assert all(value["status"] == "queued" for value in receipts)
    runs = store(tmp_path)
    assert runs.claim()[1] == 19
    assert runs.claim() is None
    assert runs.read("hanggent_u7_w12", 19)["status"] == "running"
    with pytest.raises(ValueError):
        runs.read("hanggent_u8_w12", 19)


def test_missing_credentials_is_durable_blocked_without_worker(tmp_path: Path) -> None:
    runs = store(tmp_path)
    receipt = runs.enqueue("hanggent_u7_w12", 19, "")
    assert receipt["error_code"] == "marketplace_credentials_missing"
    assert receipt["session_valid"] is False
    assert "results" not in runs.read("hanggent_u7_w12", 19, True)
    assert runs.claim() is None


@pytest.mark.parametrize(
    "exit_code,output,status",
    [
        (0, {"session_valid": True, "results": []}, "completed"),
        (0, {"session_valid": False, "results": []}, "failed"),
        (0, None, "failed"),
        (1, None, "failed"),
        (2, None, "paused_needs_attention"),
    ],
)
def test_exit_alone_cannot_complete_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    output: dict | None,
    status: str,
) -> None:
    runs = store(tmp_path)
    runs.enqueue("hanggent_u7_w12", 19, CONFIG)

    def process(args: list[str], **kwargs: Any) -> Any:
        assert "AIMM_HOME" in kwargs["env"]
        assert "private-password" in Path(args[-2]).read_text(encoding="utf-8")
        if output is not None:
            Path(args[-1]).write_text(json.dumps(output), encoding="utf-8")

        class Process:
            def wait(self, **kwargs: Any) -> int:
                return exit_code

        return Process()

    monkeypatch.setattr(subprocess, "Popen", process)
    runs.execute(*runs.claim())
    receipt = store(tmp_path).read("hanggent_u7_w12", 19, True)
    assert receipt["status"] == status
    assert "private-password" not in json.dumps(receipt)
    if status == "completed":
        assert receipt["results_digest"] == digest(
            {"user_id": 7, "watch_id": 12, "run_id": 19}, []
        )
        assert receipt["results"] == []
    else:
        assert "results" not in receipt
    with runs.connect() as db:
        assert db.execute("SELECT config FROM jobs").fetchone()[0] == ""


def test_user_queue_serialization_and_expired_worker(tmp_path: Path) -> None:
    runs = store(tmp_path)
    runs.enqueue("hanggent_u7_w12", 19, CONFIG)
    runs.enqueue("hanggent_u7_w12", 20, CONFIG)
    runs.enqueue("hanggent_u8_w12", 19, CONFIG)
    assert runs.claim()[0] == "hanggent_u7_w12"
    assert runs.claim()[0] == "hanggent_u8_w12"
    assert runs.claim() is None
    with runs.connect() as db:
        db.execute("UPDATE jobs SET deadline=0 WHERE run=19")
    assert runs.claim()[1] == 20
    assert runs.read("hanggent_u7_w12", 19)["error_code"] == "source_worker_interrupted"


def test_timeout_kills_worker_and_never_seals_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = store(tmp_path)
    runs.enqueue("hanggent_u7_w12", 19, CONFIG)
    killed = []

    class Process:
        pid = 12345

        def wait(self, timeout: int | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9

        def kill(self) -> None:
            killed.append(True)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    import os

    if os.name != "nt":
        monkeypatch.setattr(os, "killpg", lambda *args: killed.append(True))
    runs.execute(*runs.claim())
    receipt = runs.read("hanggent_u7_w12", 19, True)
    assert killed == [True]
    assert receipt["error_code"] == "source_worker_timeout"
    assert receipt["status"] == "failed"
    assert "results" not in receipt
