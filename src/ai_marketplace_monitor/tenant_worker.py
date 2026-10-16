"""One-shot, tenant-isolated Hanggent worker."""

from __future__ import annotations

import logging
import sys

from .monitor import MarketplaceMonitor


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: tenant_worker WATCH_KEY")
    watch_key = sys.argv[1]
    logger = logging.getLogger(f"hanggent.{watch_key}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    monitor: MarketplaceMonitor | None = None
    try:
        monitor = MarketplaceMonitor(None, True, logger)
        monitor.run_item_once(watch_key)
        return 0
    except Exception:
        logger.exception("Tenant Marketplace Monitor scan failed")
        return 1
    finally:
        if monitor is not None:
            monitor.stop_monitor()


if __name__ == "__main__":
    raise SystemExit(main())
