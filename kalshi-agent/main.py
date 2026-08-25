"""Agent entry point.

Right now it runs the Step 1 deliverable: load configuration, build an
authenticated client and verify the connection. The monitor loop is added in
Step 2 once market.py lands.
"""

from __future__ import annotations

import sys

from auth import KalshiError, build_client
from config import ConfigError
from logging_config import configure_logging, get_logger

logger = get_logger("main")


def main() -> int:
    configure_logging()
    logger.info("Starting Kalshi agent")
    try:
        with build_client() as client:
            summary = client.verify_connection()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except KalshiError as exc:
        logger.error("Kalshi API error: %s", exc)
        return 1

    logger.info("Startup checks complete | %s", summary)
    logger.info("Next step: market data retrieval (market.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
