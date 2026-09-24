"""Private Hanggent integration for namespaced Marketplace Monitor watches."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from .config_api import ConfigFileService, scan_sections

WATCH_KEY_RE = re.compile(r"^hanggent_u[1-9][0-9]*_w[1-9][0-9]*$")


@dataclass(frozen=True)
class HanggentWatch:
    search_phrases: list[str]
    search_region: str
    search_interval: str = "60m"
    min_price: int | None = None
    max_price: int | None = None
    enabled: bool = True


class HanggentIntegrationService:
    """Own only ``[item.hanggent_*]`` config sections.

    ConfigFileService performs validation, atomic writes, and secret restoration,
    so the integration cannot accidentally replace Facebook credentials with the
    redaction marker returned by ``read``.
    """

    def __init__(self, config_service: ConfigFileService) -> None:
        self._config = config_service
        self._lock = threading.Lock()

    @staticmethod
    def validate_key(watch_key: str) -> None:
        if not WATCH_KEY_RE.fullmatch(watch_key):
            raise ValueError("Invalid Hanggent watch key")

    def upsert(self, watch_key: str, watch: HanggentWatch) -> None:
        self.validate_key(watch_key)
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
        self._replace_section(watch_key, "\n".join(section) + "\n")

    def delete(self, watch_key: str) -> None:
        self.validate_key(watch_key)
        self._replace_section(watch_key, None)

    def wake(self) -> None:
        self._config.editable_path.touch()

    def _replace_section(self, watch_key: str, replacement: str | None) -> None:
        target = f"item.{watch_key}"
        with self._lock:
            for _ in range(3):
                content, mtime = self._config.read("primary")
                lines = content.splitlines(keepends=True)
                sections = scan_sections(content)
                section = next((s for s in sections if s.name == target), None)
                effective_replacement = replacement
                if (
                    replacement is None
                    and section is not None
                    and not any(s.prefix == "item" and s.name != target for s in sections)
                ):
                    # AIMM requires at least one item. Keep an inert tombstone if
                    # this is the final one rather than making the whole config invalid.
                    block = "".join(lines[section.line_start : section.line_end]).rstrip()
                    if re.search(r"(?m)^enabled\s*=", block):
                        block = re.sub(r"(?m)^enabled\s*=.*$", "enabled = false", block)
                    else:
                        block += "\nenabled = false"
                    effective_replacement = block + "\n"
                if section is not None:
                    del lines[section.line_start : section.line_end]
                updated = "".join(lines).rstrip()
                if effective_replacement is not None:
                    updated = (
                        f"{updated}\n\n{effective_replacement}"
                        if updated
                        else effective_replacement
                    )
                elif updated:
                    updated += "\n"
                _, ok, error = self._config.write("primary", updated, mtime)
                if ok:
                    return
                if not error or "conflict" not in error:
                    raise ValueError(error or "Failed to update Marketplace Monitor config")
            raise RuntimeError("Marketplace Monitor config changed repeatedly; retry later")
