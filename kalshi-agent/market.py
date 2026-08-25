"""Market data retrieval and parsing.  (Step 2 - not yet implemented.)

Planned surface, built on the authenticated client from auth.py:

    get_series(series_ticker)          -> series metadata
    list_events(series_ticker)         -> open events in the series
    list_markets(event_ticker)         -> markets, with pagination handled
    get_orderbook(market_ticker)       -> top-of-book bid/ask in cents
    snapshot(series_ticker)            -> normalised dataclasses for strategy.py

Prices come back from Kalshi in cents (1-99); this module will be the single
place that converts them to probabilities so no other module has to.
"""

from __future__ import annotations

from auth import KalshiClient
from logging_config import get_logger

logger = get_logger("market")


def snapshot(client: KalshiClient, series_ticker: str) -> dict:
    raise NotImplementedError("market.py is Step 2 of the build - see README.md")
