"""Order placement with dry-run protection.  (Step 4 - not yet implemented.)

Planned surface:

    place_order(client, signal)   -> logs the intended order and returns a
                                     simulated receipt when client.dry_run is
                                     True; otherwise POSTs /portfolio/orders
                                     with a client_order_id for idempotency.
    cancel_order(client, order_id)

The dry-run gate lives here and is checked against client.dry_run (which comes
from KALSHI_DRY_RUN and defaults to True) before any write endpoint is called.
"""

from __future__ import annotations

from auth import KalshiClient
from logging_config import get_logger

logger = get_logger("execution")


def place_order(client: KalshiClient, signal) -> dict:
    raise NotImplementedError("execution.py is Step 4 of the build - see README.md")
