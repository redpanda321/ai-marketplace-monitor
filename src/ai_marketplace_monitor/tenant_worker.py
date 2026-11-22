"""One-shot, tenant-isolated Hanggent worker."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .monitor import MarketplaceMonitor


def main() -> int:
    if len(sys.argv) not in (2, 4):
        raise SystemExit("usage: tenant_worker WATCH_KEY")
    watch_key = sys.argv[1]
    logger = logging.getLogger(f"hanggent.{watch_key}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    monitor: MarketplaceMonitor | None = None
    try:
        scoped = len(sys.argv) == 4
        monitor = MarketplaceMonitor([Path(sys.argv[2])] if scoped else None, True, logger)
        if scoped:
            monitor.config_files = [Path(sys.argv[2])]
        rows = monitor.run_item_once(watch_key, collect_only=scoped)
        if scoped:
            Path(sys.argv[3]).write_text(
                json.dumps({"session_valid": True, "results": rows}), encoding="utf-8"
            )
        return 0
    except PermissionError:
        return 2
    except Exception:
        logger.exception("Tenant Marketplace Monitor scan failed")
        return 1
    finally:
        if monitor is not None:
            monitor.stop_monitor()


if __name__ == "__main__":
    raise SystemExit(main())
