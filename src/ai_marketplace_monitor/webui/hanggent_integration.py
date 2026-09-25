"""Private, tenant-isolated Hanggent integration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diskcache import Cache

from ..utils import amm_home
from .config_api import scan_sections
from .found_export import iter_found_rows

WATCH_KEY_RE = re.compile(r"^hanggent_u([1-9][0-9]*)_w[1-9][0-9]*$")


@dataclass(frozen=True)
class HanggentWatch:
    search_phrases: list[str]
    search_region: str
    search_interval: str = "60m"
    min_price: int | None = None
    max_price: int | None = None
    enabled: bool = True


class HanggentIntegrationService:
    """Manage an isolated AIMM home, config, cache and process per user."""

    def __init__(self, root: Path | None = None) -> None:
        default_root = Path(os.environ.get("AIMM_TENANT_ROOT", str(amm_home / "users")))
        self._root = (root or default_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._queues: dict[int, list[str]] = {}
        self._workers: dict[int, threading.Thread] = {}

    @staticmethod
    def user_id_for_key(watch_key: str) -> int:
        match = WATCH_KEY_RE.fullmatch(watch_key)
        if not match:
            raise ValueError("Invalid Hanggent watch key")
        return int(match.group(1))

    @classmethod
    def validate_key(cls, watch_key: str) -> None:
        cls.user_id_for_key(watch_key)

    def update_settings(self, user_id: int, settings: dict[str, Any]) -> None:
        if user_id < 1:
            raise ValueError("Invalid Hanggent user")
        username = str(settings.get("facebook_username") or "").strip()
        password = str(settings.get("facebook_password") or "")
        if not username or not password:
            raise ValueError("Facebook username and password are required")
        with self._lock:
            existing = self._read(user_id)
            item_blocks = self._item_blocks(existing)
            content = self._settings_toml(settings)
            if item_blocks:
                content += "\n" + "\n\n".join(item_blocks) + "\n"
            self._write(user_id, content)

    def upsert(self, watch_key: str, watch: HanggentWatch) -> None:
        user_id = self.user_id_for_key(watch_key)
        phrases = [phrase.strip() for phrase in watch.search_phrases if phrase.strip()]
        if not phrases:
            raise ValueError("At least one search phrase is required")
        if watch.search_region not in {"usa", "can"}:
            raise ValueError("search_region must be 'usa' or 'can'")
        fields: list[tuple[str, Any]] = [
            ("search_phrases", phrases),
            ("search_region", watch.search_region),
            ("search_interval", watch.search_interval),
            ("marketplace", "facebook"),
            ("enabled", watch.enabled),
        ]
        if watch.min_price is not None:
            fields.append(("min_price", watch.min_price))
        if watch.max_price is not None:
            fields.append(("max_price", watch.max_price))
        section = [f"[item.{watch_key}]"]
        section.extend(f"{key} = {json.dumps(value)}" for key, value in fields)
        self._replace_item(user_id, watch_key, "\n".join(section) + "\n")

    def delete(self, watch_key: str) -> None:
        self._replace_item(self.user_id_for_key(watch_key), watch_key, None)

    def run(self, watch_key: str) -> bool:
        user_id = self.user_id_for_key(watch_key)
        if not self._config_path(user_id).exists():
            raise ValueError("Marketplace Monitor account settings are not configured")
        with self._lock:
            queue = self._queues.setdefault(user_id, [])
            if watch_key not in queue:
                queue.append(watch_key)
            current = self._workers.get(user_id)
            if current is not None and current.is_alive():
                return False
            worker = threading.Thread(
                target=self._run_queue,
                args=(user_id,),
                name=f"aimm-user-{user_id}",
                daemon=True,
            )
            self._workers[user_id] = worker
            worker.start()
        return True

    def _run_queue(self, user_id: int) -> None:
        while True:
            with self._lock:
                queue = self._queues.setdefault(user_id, [])
                if not queue:
                    self._workers.pop(user_id, None)
                    return
                watch_key = queue.pop(0)
            env = os.environ.copy()
            env["AIMM_HOME"] = str(self._user_home(user_id))
            subprocess.run(
                [sys.executable, "-m", "ai_marketplace_monitor.tenant_worker", watch_key],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                check=False,
            )

    def results(self, watch_key: str, limit: int) -> list[dict[str, str]]:
        user_id = self.user_id_for_key(watch_key)
        home = self._user_home(user_id)
        if not home.exists():
            return []
        with Cache(home) as tenant_cache:
            rows = [row for row in iter_found_rows(tenant_cache) if row.get("item") == watch_key]
        return rows[-limit:]

    def _user_home(self, user_id: int) -> Path:
        return self._root / f"u{user_id}"

    def _config_path(self, user_id: int) -> Path:
        return self._user_home(user_id) / "config.toml"

    def _read(self, user_id: int) -> str:
        path = self._config_path(user_id)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _write(self, user_id: int, content: str) -> None:
        home = self._user_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        try:
            home.chmod(0o700)
        except OSError:
            pass
        path = self._config_path(user_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)

    @staticmethod
    def _item_blocks(content: str) -> list[str]:
        lines = content.splitlines(keepends=True)
        return [
            "".join(lines[section.line_start : section.line_end]).rstrip()
            for section in scan_sections(content)
            if section.prefix == "item"
        ]

    def _replace_item(self, user_id: int, watch_key: str, replacement: str | None) -> None:
        with self._lock:
            content = self._read(user_id)
            lines = content.splitlines(keepends=True)
            target = f"item.{watch_key}"
            section = next((value for value in scan_sections(content) if value.name == target), None)
            if section is not None:
                del lines[section.line_start : section.line_end]
            updated = "".join(lines).rstrip()
            if replacement:
                updated = f"{updated}\n\n{replacement}" if updated else replacement
            self._write(user_id, updated)

    @staticmethod
    def _settings_toml(settings: dict[str, Any]) -> str:
        def value(name: str, default: Any = "") -> str:
            return json.dumps(settings.get(name, default))

        rows = [
            "[marketplace.facebook]",
            f"username = {value('facebook_username')}",
            f"password = {value('facebook_password')}",
            f"search_city = {value('search_city')}",
            f"login_wait_time = {value('login_wait_time', 60)}",
            f"search_interval = {value('search_interval', '60m')}",
            f"max_search_interval = {value('max_search_interval', '120m')}",
            f"seller_locations = {value('seller_locations', [])}",
            f"exclude_sellers = {value('exclude_sellers', [])}",
            'notify = "hanggent"',
            "",
            "[user.hanggent]",
            "notify_with = []",
        ]
        notification_email = str(settings.get("notification_email") or "").strip()
        if notification_email:
            rows.insert(rows.index("notify_with = []"), f"email = {json.dumps(notification_email)}")
        provider = str(settings.get("ai_provider") or "").strip().lower()
        api_key = str(settings.get("ai_api_key") or "")
        if provider and api_key:
            rows.extend(
                [
                    "",
                    f"[ai.{provider}]",
                    f"provider = {json.dumps(provider)}",
                    f"api_key = {json.dumps(api_key)}",
                    f"model = {value('ai_model')}",
                ]
            )
        return "\n".join(rows).rstrip() + "\n"
