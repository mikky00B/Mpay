# Mpay

**A self-hostable crypto payment engine.** Mpay makes *"this invoice is paid"* a trustworthy, automatic statement — idempotent matching of on-chain transfers to invoices, an explicit payment state machine that understands confirmations instead of a boolean `paid` flag, and merchant webhooks that are cryptographically verifiable and retried until they land.

v1 settles **USDC on Ethereum** (EVM). No smart contracts — Mpay consumes the chain, it doesn't extend it. Funds move payer-to-merchant directly on-chain; Mpay is the verification and notification layer, never a custodian.

## Why

A static wallet address doesn't survive contact with real volume:

| Static address | With Mpay |
|---|---|
| *"Which payment is this?"* — one address, ten clients, manual reconciliation | Every invoice carries a unique payable amount; incoming transfers are matched automatically and exactly-once |
| Watching a block explorer by hand to release digital goods | Confirmation-depth tracking advances the invoice through its lifecycle and fires a webhook the moment it's trustworthy |
| Fire-and-forget webhooks that lose orders when an endpoint hiccups | Every delivery attempt is logged, HMAC-signed, and retried with exponential backoff |
| *"Paid"* is a guess when transactions sit unconfirmed or get orphaned | Payments are continuously re-verified against the chain; a reorged payment is detected, rolled back, and the merchant is notified |

Mpay is the reliability core of a crypto invoicing platform — the part that isn't the checkout page.

## Documentation

| | |
|---|---|
| **[Live documentation site](http://localhost:8000/docs)** | Served by every Mpay instance at `/docs` — guides, API reference, search |
| [Quickstart](docs/quickstart.md) | From `git clone` to a live invoice in ~10 minutes |
| [API reference](docs/api.md) | Endpoints, semantics, error codes |
| [Checkout & payment links](docs/checkout.md) | What payers see; the unique-amount matching model |
| [Webhooks](docs/webhooks.md) | Events, HMAC verification, retry semantics, debugging |
| [Operations](docs/operations.md) | Configuration, running as services, failure behavior |
| [Brand](docs/brand.md) | Logo, palette, and typography specs |

## How a payment flows

```
CREATED → AWAITING_PAYMENT → DETECTED → CONFIRMING → CONFIRMED → SETTLED
                                  \                        │
                                   (TTL expired)           (reorg detected)
                                   ▼                       ▼
                               EXPIRED ←────── AWAITING_PAYMENT (rollback)
```

1. **Invoice creation** — the API mints an invoice with a unique payable amount on the receiving address. Amount matching means no two open invoices are ever ambiguous.
2. **Watch** — a standalone Go watcher tails `eth_getLogs` for USDC `Transfer` events to the hot address, checkpointed block-by-block, and ingests raw events through an internal API.
3. **Process** — the event processor applies events exactly-once: dedup is enforced by a database-level `UNIQUE (tx_hash, log_index)` on both `chain_events` and `payments`, so a replayed event is structurally unrepresentable, not merely filtered.
4. **Confirm** — a sweep advances invoices once the payment is `CONFIRMATION_THRESHOLD` blocks deep.
5. **Notify** — the dispatcher delivers `invoice.confirmed` (HMAC-SHA256-signed, `X-Mpay-Signature` header). Successful delivery settles the invoice and fires `invoice.settled`.
6. **Verify** — a reconciliation job continuously cross-checks the database against the chain: stuck notifications are rescued, dead processors are flagged, and payments that vanish from the chain inside the reorg-safety window are rolled back and the merchant told (`invoice.reorgged`).

## Architecture

```
Merchant backend ──REST──▶ Mpay API (FastAPI) ──webhooks──▶ Merchant backend
                            │   SQLAlchemy 2.0 · SQLite/PostgreSQL
                            │
                            ├── event processor      (exactly-once credits)
                            ├── confirmation sweep   (depth + TTL expiry)
                            ├── webhook dispatcher   (backoff, audit log)
                            └── reconciliation       (DB vs chain, reorgs)
                                      ▲
Go watcher (eth_getLogs, checkpointed) ── POST /internal/events ──┘
```

The Go watcher is a separate process by design: it keeps writing events while the API is down, and its checkpoint only advances after successful ingest, so crashes resume exactly-once.

## Quickstart

Requirements: Python 3.11+, Go 1.21+ (watcher), an EVM RPC endpoint.

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -c "import app.models, app.db as d; d.Base.metadata.create_all(d.get_engine())"
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Build and start the watcher (`RPC_URL`, `API_URL`, `RECEIVING_ADDRESS` = your chain, API, and hot wallet):

```bash
cd watcher && go build -o mpay-watcher.exe .
RPC_URL=https://... API_URL=http://127.0.0.1:8000 \
RECEIVING_ADDRESS=0x... USDC_CONTRACT=0x... \
INTERNAL_API_KEY=... MAX_BLOCKS=10 ./mpay-watcher.exe
```

> `MAX_BLOCKS` bounds the `eth_getLogs` block range per call — RPC providers cap it (Alchemy's free tier: 10). The watcher splits long catch-up ranges into windows automatically.

Create a merchant and an invoice:

```bash
curl -X POST http://127.0.0.1:8000/merchants -H "Content-Type: application/json" \
  -d '{"name":"Acme","webhook_url":"https://your-endpoint/hook"}'
# -> {"id":1, ..., "webhook_secret":"whsec_...", "api_key":"mpay_sk_..."}   (shown ONCE)

curl -X POST http://127.0.0.1:8000/merchants/1/invoices -H "Content-Type: application/json" \
     -H "X-API-Key: mpay_sk_..." \
  -d '{"amount":"25.50","idempotency_key":"order-42"}'
```

Merchant-scoped endpoints require the `X-API-Key` header (keys are stored hashed; the raw value is shown once at creation). The invoice response includes a `public_id` — the hosted checkout page for that invoice lives at **`/pay/{public_id}`**: a payment link with a scannable EIP-681 QR (wallet apps pre-fill token and exact amount) and live status.

The response's `payable_amount` is what the payer sends — the per-invoice unique amount is the matching key. When the transfer confirms, `https://your-endpoint/hook` receives a signed `invoice.confirmed`.

### Verifying webhooks

Compute HMAC-SHA256 over the **raw request body** with your merchant's `webhook_secret` (shown once at merchant creation) and compare against the `X-Mpay-Signature` header:

```python
import hmac, hashlib
expected = hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Mpay-Signature"])
```

## Configuration

All settings are environment variables (see `app/config.py`); none are required for local dev with SQLite.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./mpay.db` | `postgresql+psycopg://…` in production |
| `RECEIVING_ADDRESS` | zero address | Hot wallet all invoices point at (lowercased automatically) |
| `USDC_CONTRACT` | mainnet USDC | Token contract to watch |
| `CONFIRMATION_THRESHOLD` | `12` | Blocks before `CONFIRMING → CONFIRMED` |
| `INVOICE_TTL_MINUTES` | `60` | Unpaid invoices expire |
| `RPC_URL` | *(empty = offline)* | Chain endpoint for confirmation depth + reconciliation |
| `INTERNAL_API_KEY` | `dev-internal-key` | Shared secret between watcher and ingest API |
| `WEBHOOK_MAX_ATTEMPTS` / `WEBHOOK_BACKOFF_*` | `5` / `2s, cap 60s` | Delivery retry policy |
| `REORG_SAFETY_DEPTH` | `60` | Window in which a vanished payment is rolled back |
| `WEBHOOK_ALLOW_PRIVATE_HOSTS` | `false` | SSRF guard escape hatch — loopback webhook targets (dev rigs only) |

## Testing

```bash
pytest -q                    # behavior tests against a real (temp-file) DB
python scripts/smoke_e2e.py  # full system: real API + real watcher + mock chain
```

The smoke test drives the actual binaries end-to-end — invoice → on-chain transfer → confirmations → signed webhook (after a simulated outage) → settlement → replay dedupe.

## Security model

- **Non-custodial:** payments move payer-to-merchant directly on-chain; Mpay never holds funds.
- Webhook secrets are generated server-side, returned once, and verified with constant-time comparison.
- The internal ingest endpoint is guarded by `X-Internal-Key` and rejects unauthenticated requests before body validation.
- Money crosses the API only as exact decimal strings; conversion to integer base units happens in one audited module (`app/money.py`) — never floats.
- Addresses are normalized to lowercase at every boundary; matching is case-exact at the database level.

## Roadmap

- [x] Exactly-once event processing, confirmation tracking, signed webhook delivery
- [x] Reconciliation job (stuck-confirmation rescue, chain cross-check)
- [x] Reorg handling (payment rollback + merchant notification)
- [x] Merchant API keys + hosted checkout page (payment link with QR)
- [ ] Over/underpayment policy
- [ ] Second token / chain

## License

MIT — see [LICENSE](LICENSE).
