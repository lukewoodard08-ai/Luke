"""Offline tests for auth.py - no network, no real credentials.

Run from the project root:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from auth import KalshiAuthError, KalshiClient, KalshiRateLimitError, KalshiSigner
from config import Settings


def _keypair() -> tuple[bytes, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, key.public_key()


def _settings(pem: bytes, **overrides) -> Settings:
    base = dict(
        environment="demo",
        base_url="https://demo-api.kalshi.co/trade-api/v2",
        api_key_id="test-key-id",
        private_key_pem=pem,
        dry_run=True,
        max_retries=2,
        backoff_base=1.0,
        backoff_cap=0.0,  # keep tests instant
    )
    base.update(overrides)
    return Settings(**base)


class SignerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pem, self.public_key = _keypair()
        self.signer = KalshiSigner("test-key-id", self.pem)

    def test_signature_verifies_against_public_key(self) -> None:
        timestamp, method, path = "1750000000000", "GET", "/trade-api/v2/portfolio/balance"
        signature = base64.b64decode(self.signer.sign(timestamp, method, path))

        # Raises InvalidSignature if the payload or padding is wrong.
        self.public_key.verify(
            signature,
            f"{timestamp}{method}{path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )

    def test_headers_sign_path_only_not_query(self) -> None:
        headers = self.signer.headers("GET", "https://demo-api.kalshi.co/trade-api/v2/markets?limit=5&cursor=abc")
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        self.public_key.verify(
            signature,
            f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "test-key-id")

    def test_malformed_key_raises_auth_error(self) -> None:
        with self.assertRaises(KalshiAuthError):
            KalshiSigner("test-key-id", b"-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")


def _response(status: int, payload=None, headers=None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b"" if payload is None else __import__("json").dumps(payload).encode()
    return response


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pem, _ = _keypair()
        self.session = mock.Mock(spec=requests.Session)
        self.session.headers = {}
        self.client = KalshiClient(_settings(self.pem), session=self.session)

    def test_get_returns_decoded_body(self) -> None:
        self.session.request.return_value = _response(200, {"balance": 12345})
        self.assertEqual(self.client.get("/portfolio/balance"), {"balance": 12345})

    def test_rate_limit_is_retried_then_succeeds(self) -> None:
        self.session.request.side_effect = [
            _response(429, {"message": "slow down"}, {"Retry-After": "0"}),
            _response(200, {"markets": []}),
        ]
        self.assertEqual(self.client.get("/markets"), {"markets": []})
        self.assertEqual(self.session.request.call_count, 2)

    def test_rate_limit_raises_after_retries_exhausted(self) -> None:
        self.session.request.return_value = _response(429, {"message": "slow down"}, {"Retry-After": "0"})
        with self.assertRaises(KalshiRateLimitError):
            self.client.get("/markets")
        self.assertEqual(self.session.request.call_count, 3)  # max_retries=2 -> 3 attempts

    def test_non_idempotent_post_is_not_retried_on_network_error(self) -> None:
        self.session.request.side_effect = requests.ConnectionError("reset by peer")
        with self.assertRaises(Exception):
            self.client.post("/portfolio/orders", {"ticker": "X"})
        self.assertEqual(self.session.request.call_count, 1)

    def test_bad_credentials_raise_auth_error(self) -> None:
        self.session.request.return_value = _response(401, {"message": "invalid signature"})
        with self.assertRaises(KalshiAuthError):
            self.client.get("/portfolio/balance")

    def test_each_attempt_is_signed_with_a_fresh_timestamp(self) -> None:
        self.session.request.side_effect = [
            _response(503, {"message": "unavailable"}),
            _response(200, {}),
        ]
        self.client.get("/exchange/status")
        first, second = self.session.request.call_args_list
        self.assertNotEqual(
            first.kwargs["headers"]["KALSHI-ACCESS-SIGNATURE"],
            second.kwargs["headers"]["KALSHI-ACCESS-SIGNATURE"],
        )


if __name__ == "__main__":
    unittest.main()
