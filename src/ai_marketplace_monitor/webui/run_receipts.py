"""Durable, run-scoped source jobs. Never treat the historical cache as a receipt."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator

from ..facebook_session import FacebookSessionStore

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(identity: dict[str, int], rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            {**identity, "results": rows}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


class RunReceipts:
    def __init__(self, root: Path, start_worker: bool = True) -> None:
        self.root = root
        self.path = root / "run-receipts.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (watch TEXT, run INTEGER, user INTEGER, status TEXT, deadline REAL, config TEXT, receipt TEXT, results TEXT, PRIMARY KEY(watch, run))"
            )
            if "session_state" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN session_state TEXT")
        self.path.chmod(0o600)
        self.thread = threading.Thread(target=self.work, daemon=True, name="aimm-receipts")
        if start_worker:
            self.thread.start()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, key: str, run_id: int, config: str) -> dict[str, Any]:
        user_id, watch_id = (int(part[1:]) for part in key.split("_")[1:])
        if type(run_id) is not int or run_id < 1:
            raise ValueError("A positive run_id is required")
        identity = {"user_id": user_id, "watch_id": watch_id, "run_id": run_id}
        credentials = tomllib.loads(config).get("marketplace", {}).get("facebook", {})
        state = FacebookSessionStore(self.root / f"u{user_id}").snapshot(config)
        missing = not state and (
            not credentials.get("username") or not credentials.get("password")
        )
        receipt = dict(
            schema_version=1,
            **identity,
            status="paused_needs_attention" if missing else "queued",
            queued_at=now(),
            session_valid=False,
            error_code="marketplace_credentials_missing" if missing else None,
        )
        if missing:
            receipt["completed_at"] = now()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO jobs (watch,run,user,status,deadline,config,receipt,results,session_state) VALUES (?,?,?,?,NULL,?,?,NULL,?)",
                (
                    key,
                    run_id,
                    user_id,
                    receipt["status"],
                    "" if missing else config,
                    json.dumps(receipt),
                    state,
                ),
            )
        return self.read(key, run_id)

    @contextmanager
    def account_update(self, user_id: int, cancel_pending: bool = False) -> Iterator[None]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM jobs WHERE user=? AND status='running'", (user_id,)
            ).fetchone():
                raise ValueError("marketplace_scan_in_progress")
            yield
            if cancel_pending:
                for watch, run_id, raw in db.execute(
                    "SELECT watch,run,receipt FROM jobs WHERE user=? AND status='queued'",
                    (user_id,),
                ).fetchall():
                    receipt = json.loads(raw)
                    receipt.update(
                        status="paused_needs_attention",
                        error_code="marketplace_account_changed",
                        session_valid=False,
                        completed_at=now(),
                    )
                    db.execute(
                        "UPDATE jobs SET status='paused_needs_attention',config='',session_state=NULL,receipt=? WHERE watch=? AND run=?",
                        (json.dumps(receipt), watch, run_id),
                    )

    def read(self, key: str, run_id: int, results: bool = False) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT receipt, results FROM jobs WHERE watch=? AND run=?", (key, run_id)
            ).fetchone()
        if row is None:
            raise ValueError("Source run receipt not found")
        receipt = json.loads(row[0])
        if results and receipt["status"] == "completed":
            receipt["results"] = json.loads(row[1])
        return receipt

    def claim(self) -> tuple[str, int, str, dict[str, Any]] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, run_id, raw in db.execute(
                "SELECT watch,run,receipt FROM jobs WHERE status='running' AND deadline < ?",
                (time.time(),),
            ).fetchall():
                receipt = json.loads(raw)
                receipt.update(
                    status="failed", error_code="source_worker_interrupted", completed_at=now()
                )
                db.execute(
                    "UPDATE jobs SET status='failed', receipt=?, config='', session_state=NULL WHERE watch=? AND run=?",
                    (json.dumps(receipt), key, run_id),
                )
            row = db.execute(
                "SELECT watch,run,config,receipt FROM jobs j WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM jobs a WHERE a.user=j.user AND a.status='running') ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row:
                receipt = json.loads(row[3])
                receipt.update(status="running", started_at=now())
                db.execute(
                    "UPDATE jobs SET status='running',deadline=?,receipt=? WHERE watch=? AND run=?",
                    (time.time() + self.timeout() + 30, json.dumps(receipt), row[0], row[1]),
                )
                return (*row[:3], receipt)

    @staticmethod
    def timeout() -> int:
        return max(5, min(900, int(os.getenv("AIMM_RUN_TIMEOUT_SECONDS", "300"))))

    def work(self) -> None:
        while True:
            try:
                job = self.claim()
                if job:
                    self.execute(*job)
                    continue
            except Exception:
                # Leave interrupted jobs durable; leases expire without false success.
                logging.getLogger(__name__).warning(
                    "Source receipt worker could not process its queue"
                )
            time.sleep(1)

    def execute(self, key: str, run_id: int, config: str, receipt: dict[str, Any]) -> None:
        rows = None
        receipt.update(status="failed", error_code="source_worker_failed")
        try:
            home = self.root / f"u{receipt['user_id']}"
            with TemporaryDirectory(prefix="run-", dir=home) as temporary:
                folder = Path(temporary)
                folder.chmod(0o700)
                snapshot = folder / "config.toml"
                snapshot.write_text(config, encoding="utf-8")
                snapshot.chmod(0o600)
                output = folder / "output.json"
                with self.connect() as db:
                    state = db.execute(
                        "SELECT session_state FROM jobs WHERE watch=? AND run=?", (key, run_id)
                    ).fetchone()[0]
                session_file = folder / "session.json"
                if state:
                    session_file.write_text(state, encoding="utf-8")
                    session_file.chmod(0o600)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ai_marketplace_monitor.tenant_worker",
                        key,
                        str(snapshot),
                        str(output),
                    ],
                    env={
                        **os.environ,
                        "AIMM_HOME": str(home),
                        "FACEBOOK_USERNAME": "",
                        "FACEBOOK_PASSWORD": "",
                        "AIMM_FACEBOOK_SESSION_FILE": str(session_file) if state else "",
                    },
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                )
                try:
                    code = process.wait(timeout=self.timeout())
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                    receipt["error_code"] = "source_worker_timeout"
                else:
                    if code == 0 and output.exists():
                        result = json.loads(output.read_text(encoding="utf-8"))
                        if result.get("session_valid") is True and isinstance(
                            result.get("results"), list
                        ):
                            rows = result["results"]
                            identity = {
                                field: receipt[field]
                                for field in ("user_id", "watch_id", "run_id")
                            }
                            receipt.update(
                                status="completed",
                                error_code=None,
                                session_valid=True,
                                result_count=len(rows),
                                results_digest=digest(identity, rows),
                            )
                    elif code == 2:
                        if state:
                            FacebookSessionStore(home).invalidate_snapshot(state, config)
                        receipt.update(
                            status="paused_needs_attention",
                            error_code="marketplace_session_unverified",
                        )
        except Exception:
            receipt["error_code"] = "source_worker_failed"
        receipt["completed_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,receipt=?,results=?,config='',session_state=NULL WHERE watch=? AND run=? AND status='running'",
                (
                    receipt["status"],
                    json.dumps(receipt),
                    json.dumps(rows) if rows is not None else None,
                    key,
                    run_id,
                ),
            )
