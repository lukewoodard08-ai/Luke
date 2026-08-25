"""Trade trigger logic.  (Step 3 - not yet implemented.)

Planned surface: pure functions over the snapshot produced by market.py, with
no I/O, so the rules can be unit-tested without touching the network.

    evaluate(snapshot, positions) -> list[TradeSignal]

A TradeSignal carries market ticker, side, count, limit price and the human
readable reason it fired, which execution.py logs verbatim in dry-run mode.
"""

from __future__ import annotations

from logging_config import get_logger

logger = get_logger("strategy")


def evaluate(snapshot: dict, positions: dict | None = None) -> list:
    raise NotImplementedError("strategy.py is Step 3 of the build - see README.md")
