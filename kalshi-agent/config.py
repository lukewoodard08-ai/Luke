"""Environment-driven configuration.

Nothing here is hardcoded: every credential and tunable is read from the
process environment via os.getenv, which is what Base44's secret manager
injects at runtime. A local .env is honoured only as a developer convenience.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from logging_config import get_logger

logger = get_logger("config")

# Kalshi publishes a production and a demo cluster. Demo is the default so a
# misconfigured deployment can never touch a real account by accident.
API_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigError(f"{name} must be a boolean-ish value, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _load_dotenv_if_available() -> None:
    """Load a local .env when python-dotenv is installed. Never required."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    if load_dotenv(override=False):
        logger.debug("Loaded local .env file for development")


def _read_private_key_pem() -> bytes:
    """Resolve the RSA private key from env, either inline or by path.

    KALSHI_PRIVATE_KEY holds the PEM body directly (secret managers often
    collapse newlines to the two-character sequence \\n, so we restore them).
    KALSHI_PRIVATE_KEY_PATH points at a PEM file on disk instead.
    """
    inline = os.getenv("KALSHI_PRIVATE_KEY")
    if inline and inline.strip():
        return inline.strip().replace("\\n", "\n").encode("utf-8")

    path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if path and path.strip():
        expanded = os.path.expanduser(path.strip())
        if not os.path.isfile(expanded):
            raise ConfigError(f"KALSHI_PRIVATE_KEY_PATH does not point at a file: {expanded}")
        with open(expanded, "rb") as handle:
            return handle.read()

    raise ConfigError(
        "No private key configured. Set KALSHI_PRIVATE_KEY (PEM contents) or "
        "KALSHI_PRIVATE_KEY_PATH (path to the .pem downloaded from Kalshi)."
    )


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the agent's runtime configuration."""

    environment: str
    base_url: str
    api_key_id: str
    private_key_pem: bytes = field(repr=False)
    private_key_passphrase: bytes | None = field(default=None, repr=False)

    # Safety
    dry_run: bool = True

    # Resilience
    request_timeout: float = 15.0
    max_retries: int = 4
    backoff_base: float = 1.5
    backoff_cap: float = 30.0

    # Objective: the series the monitor tracks. Overridable per deployment.
    target_series: str = "KXPRESPARTY"

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv_if_available()

        environment = (os.getenv("KALSHI_ENV") or "demo").strip().lower()
        if environment not in API_BASE_URLS:
            raise ConfigError(
                f"KALSHI_ENV must be one of {sorted(API_BASE_URLS)}, got {environment!r}"
            )

        base_url = (os.getenv("KALSHI_API_BASE_URL") or API_BASE_URLS[environment]).rstrip("/")

        api_key_id = (os.getenv("KALSHI_API_KEY_ID") or "").strip()
        if not api_key_id:
            raise ConfigError(
                "KALSHI_API_KEY_ID is not set. Create an API key in the Kalshi "
                "web app (Account -> API Keys) and expose the key ID as a secret."
            )

        passphrase = os.getenv("KALSHI_PRIVATE_KEY_PASSPHRASE") or None

        settings = cls(
            environment=environment,
            base_url=base_url,
            api_key_id=api_key_id,
            private_key_pem=_read_private_key_pem(),
            private_key_passphrase=passphrase.encode("utf-8") if passphrase else None,
            dry_run=_env_bool("KALSHI_DRY_RUN", True),
            request_timeout=_env_float("KALSHI_REQUEST_TIMEOUT", 15.0),
            max_retries=_env_int("KALSHI_MAX_RETRIES", 4),
            backoff_base=_env_float("KALSHI_BACKOFF_BASE", 1.5),
            backoff_cap=_env_float("KALSHI_BACKOFF_CAP", 30.0),
            target_series=(os.getenv("KALSHI_TARGET_SERIES") or "KXPRESPARTY").strip(),
            log_level=(os.getenv("KALSHI_LOG_LEVEL") or "INFO").strip().upper(),
        )

        logger.info(
            "Configuration loaded | env=%s | base_url=%s | dry_run=%s | key_id=%s | series=%s",
            settings.environment,
            settings.base_url,
            settings.dry_run,
            settings.masked_key_id,
            settings.target_series,
        )
        if not settings.dry_run:
            logger.warning(
                "DRY RUN IS DISABLED - orders submitted by this process are REAL "
                "and will move real funds in the %s environment.",
                settings.environment,
            )
        return settings

    @property
    def masked_key_id(self) -> str:
        """Key IDs are not secret, but we still avoid printing them in full."""
        if len(self.api_key_id) <= 8:
            return "*" * len(self.api_key_id)
        return f"{self.api_key_id[:4]}...{self.api_key_id[-4:]}"

    def url_for(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"
