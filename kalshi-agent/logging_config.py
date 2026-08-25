"""Central logging setup for the Kalshi agent.

Every module gets its logger from here so that formatting, level and secret
redaction are configured in exactly one place. Base44 captures stdout/stderr,
so we deliberately log to the console rather than to files.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Patterns that must never reach a log line, even if a caller passes a raw
# header dict or an exception message that embeds a credential.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<redacted-private-key>"),
    (re.compile(r"(KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.I), r"\1<redacted>"),
    (re.compile(r"(KALSHI-ACCESS-KEY['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.I), r"\1<redacted>"),
    (re.compile(r"(KALSHI_PRIVATE_KEY[A-Z_]*['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+", re.I), r"\1<redacted>"),
)

_configured = False


class RedactingFilter(logging.Filter):
    """Scrub credential-shaped substrings from formatted log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive: never break logging
            return True

        scrubbed = message
        for pattern, replacement in _REDACTIONS:
            scrubbed = pattern.sub(replacement, scrubbed)

        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(level: str | int | None = None) -> None:
    """Install the console handler once per process.

    Safe to call from any entry point; repeat calls only adjust the level.
    """
    global _configured

    resolved = level if level is not None else os.getenv("KALSHI_LOG_LEVEL", "INFO")
    if isinstance(resolved, str):
        resolved = logging.getLevelName(resolved.upper())
    if not isinstance(resolved, int):
        resolved = logging.INFO

    root = logging.getLogger("kalshi")
    root.setLevel(resolved)

    if not _configured:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
        root.propagate = False

        # urllib3 logs full request URLs at DEBUG; keep it quiet by default.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        _configured = True
    else:
        for handler in root.handlers:
            handler.setLevel(resolved)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(f"kalshi.{name}")
