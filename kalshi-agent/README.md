# Kalshi Trading Agent

A modular, safety-first trading agent for the [Kalshi](https://kalshi.com) API,
built to run inside the Base44 environment with a minimal dependency footprint.

**Current objective:** a market monitor that tracks Presidential Election odds
(series `KXPRESPARTY`, overridable via `KALSHI_TARGET_SERIES`).

**Build status: Step 1 of 4 complete** — configuration, authentication and the
shared HTTP client are done and tested. Market data, strategy and execution are
scaffolded with their planned interfaces documented in each file.

---

## Project structure

```
kalshi-agent/
├── config.py           # env-driven settings; no credential is ever hardcoded   [DONE]
├── logging_config.py   # one console logger + credential redaction filter        [DONE]
├── auth.py             # RSA-PSS request signing + retrying HTTP client          [DONE]
├── market.py           # market discovery, orderbooks, price normalisation     [Step 2]
├── strategy.py         # pure trade-trigger logic over a market snapshot       [Step 3]
├── execution.py        # order placement behind the dry-run gate               [Step 4]
├── main.py             # entry point / agent loop
├── tests/test_auth.py  # offline tests — no network, no real credentials
├── requirements.txt
└── .env.example
```

Dependencies are just `requests` and `cryptography` (`python-dotenv` is
optional and only used for local development).

---

## How authentication works

Kalshi's `trade-api/v2` does not use bearer tokens. Every request is signed:

1. Take the current time in milliseconds.
2. Build the string `<timestamp><HTTP_METHOD><request_path>` — path only, no
   query string.
3. Sign it with your RSA private key using PSS padding and SHA-256.
4. Send `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` and
   `KALSHI-ACCESS-SIGNATURE` (base64) with the request.

Because the timestamp is inside the signed payload, signatures expire almost
immediately and cannot be replayed. Two practical consequences, both handled in
`auth.py`: every retry is re-signed with a fresh timestamp, and **the host clock
must be accurate** — clock skew shows up as a 401.

The private key is loaded once into memory and never logged. `logging_config.py`
additionally scrubs anything that looks like a PEM body or a signature header
from log output, so a stray `logger.info(headers)` cannot leak a credential.

---

## Setup

1. **Create an API key** in the Kalshi web app (Account → API Keys). You get a
   key ID and a one-time download of the private key `.pem`.

2. **Configure the environment.** Copy `.env.example` to `.env` locally, or set
   the same names as secrets in Base44:

   ```
   KALSHI_ENV=demo
   KALSHI_API_KEY_ID=<your key id>
   KALSHI_PRIVATE_KEY_PATH=/secure/path/kalshi.pem   # or KALSHI_PRIVATE_KEY=<PEM body>
   KALSHI_DRY_RUN=true
   ```

   `KALSHI_PRIVATE_KEY` accepts the PEM inline; if your secret store collapses
   newlines, literal `\n` sequences are restored automatically.

3. **Install and verify:**

   ```bash
   pip install -r requirements.txt
   python auth.py          # or: python main.py
   ```

   A healthy run logs the exchange status, then an authenticated balance read:

   ```
   ... | INFO | kalshi.config | Configuration loaded | env=demo | dry_run=True | key_id=abcd...5678
   ... | INFO | kalshi.auth   | Signer initialised | key_size=2048 bits | algorithm=RSA-PSS/SHA-256
   ... | INFO | kalshi.auth   | Exchange status | trading_active=True | exchange_active=True
   ... | INFO | kalshi.auth   | Successfully authenticated | account balance=$0.00
   ... | INFO | kalshi.auth   | Connection check PASSED | {...}
   ```

4. **Run the tests** (no credentials or network needed):

   ```bash
   python -m unittest discover -s tests -t .
   ```

---

## Safety model

| Control | Behaviour |
|---|---|
| `KALSHI_DRY_RUN` | Defaults to **true**. `execution.py` checks `client.dry_run` and logs the intended order instead of calling the write endpoint. |
| `KALSHI_ENV` | Defaults to **demo**. Production must be opted into explicitly. |
| Loud warning | Starting with dry run off logs a `WARNING` naming the environment. |
| No blind POST retries | Network failures retry only idempotent requests; a POST is retried solely when the caller marks it idempotent (i.e. it carries a `client_order_id`), so a timeout can never double-submit an order. |

---

## Resilience

`KalshiClient.request()` wraps every call:

- Retries on timeouts, connection resets and HTTP 429/500/502/503/504.
- Exponential backoff with full jitter, capped by `KALSHI_BACKOFF_CAP`; a
  `Retry-After` header always takes precedence.
- Re-signs each attempt, so retries are never rejected as stale.
- Raises a typed error — `KalshiAuthError`, `KalshiRateLimitError`,
  `KalshiAPIError`, `KalshiConnectionError` — so callers can branch on cause.
- Logs one line per request (`-> GET /markets`, `<- GET /markets 200`) plus a
  warning per retry, which is what you'll be reading in the Base44 logs.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `KALSHI_ENV` | `demo` | `demo` or `prod` — picks the API base URL |
| `KALSHI_API_BASE_URL` | derived | Override the base URL entirely |
| `KALSHI_API_KEY_ID` | *(required)* | API key ID |
| `KALSHI_PRIVATE_KEY` | — | PEM contents (alternative to the path) |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to the `.pem` file |
| `KALSHI_PRIVATE_KEY_PASSPHRASE` | — | Only for passphrase-protected keys |
| `KALSHI_DRY_RUN` | `true` | Log intended orders instead of placing them |
| `KALSHI_TARGET_SERIES` | `KXPRESPARTY` | Series the monitor tracks |
| `KALSHI_REQUEST_TIMEOUT` | `15` | Per-request timeout, seconds |
| `KALSHI_MAX_RETRIES` | `4` | Retries on top of the first attempt |
| `KALSHI_BACKOFF_BASE` | `1.5` | Exponential backoff base |
| `KALSHI_BACKOFF_CAP` | `30` | Maximum backoff, seconds |
| `KALSHI_LOG_LEVEL` | `INFO` | `DEBUG` adds per-request outbound lines |

---

## Next steps

- **Step 2 — `market.py`:** list events and markets for the target series,
  fetch orderbooks, normalise cent prices into probabilities, and return a
  snapshot dataclass.
- **Step 3 — `strategy.py`:** pure trigger functions over that snapshot,
  unit-testable without the network.
- **Step 4 — `execution.py`:** order placement with `client_order_id`
  idempotency behind the dry-run gate.

---

## Disclaimer

This is trading software. Verify behaviour in the `demo` environment with
`KALSHI_DRY_RUN=true` before pointing it at a funded account, and treat every
strategy as unproven until you have watched it run in dry-run mode.
