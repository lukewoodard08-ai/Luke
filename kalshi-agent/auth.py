"""Secure authentication and the shared HTTP client for the Kalshi API.

Kalshi's trade-api/v2 authenticates every request with an RSA-PSS signature
rather than a bearer token. For each call the client signs the string

    <timestamp_ms><HTTP_METHOD><request_path>

with the private half of an API key generated in the Kalshi web app, and sends
three headers alongside the request:

    KALSHI-ACCESS-KEY        the API key ID (public identifier)
    KALSHI-ACCESS-TIMESTAMP  the same millisecond timestamp that was signed
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS/SHA-256 signature

Because the timestamp is part of the signed payload, signatures cannot be
replayed and nothing long-lived is ever transmitted. The private key never
leaves this process and is never logged.

This module owns three things:
  * KalshiSigner  - key loading and per-request signature generation
  * KalshiClient  - a retrying, rate-limit-aware requests.Session wrapper
  * exceptions    - a small hierarchy the other modules raise and catch
"""

from __future__ import annotations

import base64
import random
import time
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config import ConfigError, Settings
from logging_config import get_logger

logger = get_logger("auth")

USER_AGENT = "kalshi-agent/0.1 (+base44)"

# Methods that can be replayed without side effects. A POST is only retried
# when the caller explicitly says the request is idempotent (i.e. it carries a
# client_order_id), so a network blip can never double-submit an order.
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE"}
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiError(RuntimeError):
    """Base class for every error this package raises."""


class KalshiAuthError(KalshiError):
    """Key material is unusable, or the API rejected our credentials."""


class KalshiAPIError(KalshiError):
    """The API returned a non-2xx response."""

    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class KalshiRateLimitError(KalshiAPIError):
    """Rate limit exceeded and retries were exhausted."""


class KalshiConnectionError(KalshiError):
    """The request never produced a response (DNS, TLS, timeout, reset)."""


class KalshiSigner:
    """Loads the RSA private key once and signs individual requests with it."""

    def __init__(self, api_key_id: str, private_key_pem: bytes, passphrase: bytes | None = None) -> None:
        self.api_key_id = api_key_id
        self._private_key = self._load_key(private_key_pem, passphrase)
        logger.info(
            "Signer initialised | key_size=%d bits | algorithm=RSA-PSS/SHA-256",
            self._private_key.key_size,
        )

    @staticmethod
    def _load_key(private_key_pem: bytes, passphrase: bytes | None) -> rsa.RSAPrivateKey:
        try:
            key = serialization.load_pem_private_key(private_key_pem, password=passphrase)
        except (ValueError, TypeError) as exc:
            # ValueError covers malformed PEM; TypeError covers a passphrase
            # supplied for an unencrypted key (or omitted for an encrypted one).
            raise KalshiAuthError(
                "Could not load the Kalshi private key. Check that the PEM is "
                "complete (including BEGIN/END lines) and that "
                "KALSHI_PRIVATE_KEY_PASSPHRASE matches the key."
            ) from exc
        except UnsupportedAlgorithm as exc:
            raise KalshiAuthError("The private key uses an algorithm this environment cannot load.") from exc

        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiAuthError(
                f"Kalshi requires an RSA private key; got {type(key).__name__}."
            )
        return key

    @staticmethod
    def timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def sign(self, timestamp: str, method: str, path: str) -> str:
        """Return the base64 RSA-PSS signature for one request."""
        message = f"{timestamp}{method.upper()}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, url: str) -> dict[str, str]:
        """Build the three signed headers for a fully-qualified request URL.

        Only the path is signed - query strings are excluded, which is why the
        signature stays valid for paginated calls that vary only by cursor.
        """
        path = urlsplit(url).path
        timestamp = self.timestamp_ms()
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": self.sign(timestamp, method, path),
        }


class KalshiClient:
    """Authenticated HTTP client with retries, backoff and structured logging.

    Every other module (market, strategy, execution) talks to Kalshi through
    this class rather than calling requests directly, so timeouts, rate-limit
    handling and logging behave identically everywhere.
    """

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.signer = KalshiSigner(
            api_key_id=self.settings.api_key_id,
            private_key_pem=self.settings.private_key_pem,
            passphrase=self.settings.private_key_passphrase,
        )
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        logger.info(
            "Kalshi client ready | env=%s | dry_run=%s",
            self.settings.environment,
            self.settings.dry_run,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self.session.close()
        logger.info("HTTP session closed")

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def dry_run(self) -> bool:
        """execution.py checks this before hitting any order endpoint."""
        return self.settings.dry_run

    # ------------------------------------------------------------------ #
    # Request plumbing
    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> dict[str, Any]:
        """Perform one signed API call and return the decoded JSON body.

        Retries transient failures with exponential backoff plus jitter.
        Raises a KalshiError subclass on permanent failure.
        """
        method = method.upper()
        url = self.settings.url_for(path)
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS

        attempts = self.settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            # Re-sign on every attempt: the timestamp is part of the payload,
            # so a stale signature would be rejected as a replay.
            headers: MutableMapping[str, str] = dict(self.signer.headers(method, url))
            if json_body is not None:
                headers["Content-Type"] = "application/json"

            logger.debug("-> %s %s (attempt %d/%d)", method, path, attempt, attempts)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self.settings.request_timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if not idempotent or attempt == attempts:
                    logger.error("Network failure on %s %s: %s", method, path, exc)
                    raise KalshiConnectionError(
                        f"{method} {path} failed to reach Kalshi: {exc}"
                    ) from exc
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "Network failure on %s %s (%s); retrying in %.1fs", method, path, exc, delay
                )
                time.sleep(delay)
                continue
            except requests.RequestException as exc:  # malformed request, bad proxy, etc.
                logger.error("Request could not be sent: %s", exc)
                raise KalshiConnectionError(str(exc)) from exc

            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                # A 429 means the request was rejected before processing, so it
                # is always safe to retry - even for a POST.
                if response.status_code == 429 or idempotent:
                    delay = self._retry_after(response) or self._backoff_delay(attempt)
                    logger.warning(
                        "Transient HTTP %d on %s %s; retrying in %.1fs (attempt %d/%d)",
                        response.status_code, method, path, delay, attempt, attempts,
                    )
                    time.sleep(delay)
                    continue

            return self._handle_response(response, method, path)

        # Defensive backstop: the loop above raises on its final attempt.
        raise KalshiConnectionError(f"{method} {path} failed after {attempts} attempts: {last_error}")

    def _handle_response(self, response: requests.Response, method: str, path: str) -> dict[str, Any]:
        if response.ok:
            logger.info("<- %s %s %d", method, path, response.status_code)
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise KalshiAPIError(
                    response.status_code, "response body was not valid JSON", response.text[:500]
                ) from exc

        payload: Any
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("error") or response.reason
        except ValueError:
            payload = response.text[:500]
            message = response.reason or "unknown error"

        logger.error("<- %s %s %d: %s", method, path, response.status_code, message)

        if response.status_code == 429:
            raise KalshiRateLimitError(429, f"rate limited: {message}", payload)
        if response.status_code in (401, 403):
            raise KalshiAuthError(
                f"Kalshi rejected our credentials (HTTP {response.status_code}: {message}). "
                "Verify KALSHI_API_KEY_ID matches the private key, that the key is "
                f"registered in the '{self.settings.environment}' environment, and that "
                "this host's clock is accurate - signatures are timestamp-bound."
            )
        raise KalshiAPIError(response.status_code, str(message), payload)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped by configuration."""
        raw = self.settings.backoff_base ** attempt
        return round(min(raw, self.settings.backoff_cap) * (0.5 + random.random() / 2), 2)

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except ValueError:
            return None  # HTTP-date form; fall back to computed backoff.

    # ------------------------------------------------------------------ #
    # Convenience verbs
    # ------------------------------------------------------------------ #
    def get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("GET", path, params=clean or None)

    def post(self, path: str, body: Mapping[str, Any], *, idempotent: bool = False) -> dict[str, Any]:
        return self.request("POST", path, json_body=body, idempotent=idempotent)

    def delete(self, path: str) -> dict[str, Any]:
        return self.request("DELETE", path)

    # ------------------------------------------------------------------ #
    # Health check
    # ------------------------------------------------------------------ #
    def verify_connection(self) -> dict[str, Any]:
        """Prove end-to-end that reachability *and* signing both work.

        /exchange/status confirms the network path and that the exchange is up;
        /portfolio/balance is an authenticated endpoint, so a 200 there means
        our signature was accepted. Returns a small summary dict.
        """
        logger.info("Verifying connection to Kalshi (%s)...", self.settings.base_url)

        status = self.get("/exchange/status")
        logger.info(
            "Exchange status | trading_active=%s | exchange_active=%s",
            status.get("trading_active"),
            status.get("exchange_active"),
        )

        balance = self.get("/portfolio/balance")
        cents = balance.get("balance", 0)
        logger.info("Successfully authenticated | account balance=$%.2f", cents / 100)

        return {
            "environment": self.settings.environment,
            "base_url": self.settings.base_url,
            "authenticated": True,
            "dry_run": self.settings.dry_run,
            "trading_active": status.get("trading_active"),
            "balance_cents": cents,
        }


def build_client(settings: Settings | None = None) -> KalshiClient:
    """Entry point used by main.py and the other modules."""
    try:
        return KalshiClient(settings)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise


if __name__ == "__main__":
    # Step 1 smoke test:  python auth.py
    import sys

    try:
        with build_client() as client:
            summary = client.verify_connection()
    except (ConfigError, KalshiError) as exc:
        logger.error("Connection check FAILED: %s", exc)
        sys.exit(1)

    logger.info("Connection check PASSED | %s", summary)
