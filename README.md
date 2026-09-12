# Mpay — Crypto Payment Gateway (USDC / EVM)

A crypto payment gateway for **USDC on Ethereum (EVM)** — the
Stripe-style invoice flow for on-chain money, without smart contracts.

Incoming USDC transfers are watched on-chain, matched to invoices, tracked
through an explicit payment state machine, and surfaced to merchants via
signed webhooks. The engineering story is **payments-infrastructure
reliability**: idempotency, exactly-once event processing, confirmation
depth, and retry-with-backoff delivery — the hard parts that make money
movement trustworthy.

> **Status: v1 complete.** 21/21 tests pass; an end-to-end smoke test drives
> a real API + real Go watcher against a mock chain and settles an invoice.
> Next milestone: Sepolia testnet run with real USDC.

---

## Architecture

```
                    ┌────────────────────────────┐
                    │   Merchant (your backend)  │
                    │   - creates invoices       │
                    │   - receives webhooks      │
                    └───────┬────────────▲───────┘
                            │ REST       │ HMAC-signed webhooks
                            ▼            │
   ┌─────────────────────────────────────────────────┐
   │                Mpay API (Python)                │
   │  FastAPI · SQLAlchemy 2.0 · PostgreSQL/SQLite   │
   │                                                 │
   │  Background loops (in-process):                 │
   │   - event processor  (credits invoices)         │
   │   - confirmation sweep (depth-based confirm)    │
   │   - webhook dispatcher (backoff + retry)        │
   └───────▲─────────────────────────────────────────┘
           │ POST /internal/events   (shared-key guarded)
           │ USDC Transfer events
   ┌───────┴─────────────────────────────────────────┐
   │            Chain watcher (Go)                   │
   │  eth_getLogs → ingest, chunked, checkpointed    │
   │  resumes exactly-once after crashes [D13]       │
   └───────▲─────────────────────────────────────────┘
           │ JSON-RPC (Alchemy / Infura / any node)
   ┌───────┴─────────────────────────────────────────┐
   │              Ethereum (EVM)                     │
   │        USDC Transfer logs, hot wallet           │
   └─────────────────────────────────────────────────┘
```

Two services, deliberately split:

- **`app/` — Python API** (FastAPI): merchants, invoices, state machine,
  idempotent event processing, confirmations, webhooks. Background loops run
  in-process for v1 (split into workers before real scale).
- **`watcher/` — Go chain watcher**: a single binary that polls `eth_getLogs`
  for USDC `Transfer` logs to the hot wallet, chunking both block ranges and
  ingest batches, persisting a checkpoint file so it resumes crash-safely.

### The invoice state machine

Invoices are never a boolean `paid` — they move through explicit states with
a single guarded transition point (`app/state_machine.py`):

```
CREATED → AWAITING_PAYMENT → CONFIRMING → CONFIRMED → SETTLED
                        └──── EXPIRED (TTL passed, no payment)
```

(Payment detection transitions straight to `CONFIRMING` at v1's confirmation
threshold; `DETECTED` is reserved for per-payment tracking.)

### Payment matching [D6][D15]

All invoices share one hot wallet address, so an incoming transfer is matched
by **(address, amount)**: each invoice's `payable_amount` = requested + its
unique per-invoice offset (the invoice id in base units), and a **partial
unique index** guarantees no two open invoices can ever present the same
`(address, amount)` key — collisions fail loudly at invoice creation (HTTP
409) instead of corrupting matching silently.

### Idempotency everywhere

| Boundary | Mechanism |
|---|---|
| Invoice creation | `idempotency_key` per merchant → same invoice returned |
| Event ingest | `UNIQUE (tx_hash, log_index)` — replays absorbed, never double-credited |
| Processing | write-first raw events, single-writer processor [D5] |
| Watcher resume | checkpoint advances only after a fully accepted scan window [D13][D16] |
| Webhooks | every attempt logged with `attempt_no`, response code, retry state |

### Webhooks [D8]

Merchants register a webhook URL and receive `invoice.confirmed` /
`invoice.settled` notifications signed with **HMAC-SHA256**
(`X-Mpay-Signature` over the raw body, using the merchant's `whsec_…`
secret, shown once at creation). Failed deliveries retry with **exponential
backoff**; every attempt is queryable via the delivery log endpoint.

---

## Quickstart

**Requirements:** Python 3.12+, Go 1.22+ (only if rebuilding the watcher).

```bash
# 1. Python API
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # POSIX

# 2. Configure (see table below)
copy .env.example .env   # or create .env manually

# 3. Run the API (background loops start with it)
.venv\Scripts\python -m uvicorn app.main:app --port 8000

# 4. Build & run the watcher
cd watcher
go build -o mpay-watcher.exe .
.\mpay-watcher.exe
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./mpay.db` | SQLAlchemy URL (Postgres in prod) |
| `RECEIVING_ADDRESS` | zero address | Hot wallet that receives payments |
| `USDC_CONTRACT` | mainnet USDC | Token contract to watch |
| `INTERNAL_API_KEY` | `dev-internal-key` | Shared key for `/internal/events` ingest |
| `CONFIRMATION_THRESHOLD` | `12` | Blocks before CONFIRMING → CONFIRMED |
| `INVOICE_TTL_MINUTES` | `60` | Expiry for unpaid invoices |
| `RPC_URL` | *(empty)* | JSON-RPC endpoint for the confirmation sweep |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | Webhook attempts before giving up |

The watcher reads its own env: `RPC_URL`, `API_URL`, `INTERNAL_API_KEY`,
`RECEIVING_ADDRESS`, `USDC_CONTRACT`, `CONFIRMATIONS`, `POLL_SECONDS`,
`MAX_BLOCKS`, `STATE_FILE`.

> **The API and watcher must agree** on `RECEIVING_ADDRESS`,
> `USDC_CONTRACT`, and `INTERNAL_API_KEY` — mismatched values are the #1
> "my payment was never detected" cause. (The E2E smoke test once failed on
> exactly this.)

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/merchants` | Create merchant; webhook secret shown **once** |
| `POST` | `/merchants/{id}/invoices` | Create invoice (idempotent via `idempotency_key`) |
| `GET` | `/invoices/{public_id}` | Invoice status + payments |
| `GET` | `/merchants/{id}/invoices` | List merchant invoices |
| `GET` | `/merchants/{id}/deliveries` | Webhook delivery log |
| `POST` | `/internal/events` | Watcher ingest (`X-Internal-Key` guarded) |
| `GET` | `/health` | Liveness |

### Example: create and pay an invoice

```bash
# 1. Create a merchant (store the secret — it is shown once)
curl -X POST http://localhost:8000/merchants \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme", "webhook_url": "https://acme.example/hooks"}'

# 2. Create an invoice
curl -X POST http://localhost:8000/merchants/1/invoices \
  -H "Content-Type: application/json" \
  -d '{"amount": "25.50", "idempotency_key": "order-42"}'
# → { "invoice": { "payable_amount": "25.500003", "receiving_address": "0x…",
#                  "status": "AWAITING_PAYMENT", "expires_at": "…" } }

# 3. The payer sends exactly `payable_amount` USDC to `receiving_address`.
#    The watcher ingests the Transfer; the processor matches (address, amount)
#    → CONFIRMING → (after CONFIRMATION_THRESHOLD blocks) → CONFIRMED → SETTLED.

# 4. Check status
curl http://localhost:8000/invoices/{public_id}
```

### Verifying a webhook (merchant side)

```python
import hmac, hashlib, flask  # any framework works

raw = flask.request.get_data()                      # exact bytes, before parsing
sig = flask.request.headers.get("X-Mpay-Signature", "")
expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, sig)           # then parse JSON
```

---

## Testing

```bash
# Unit / behavior tests (21): state machine, API, processor, confirmations,
# webhooks, plus regressions for the amount-collision and chunking fixes.
.venv\Scripts\python -m pytest tests/ -q

# End-to-end smoke: real uvicorn + the compiled Go watcher + a mock JSON-RPC
# chain + an HMAC-verifying webhook receiver. Proves the full pipeline:
# AWAITING_PAYMENT → CONFIRMED → SETTLED, 503-retry delivered, replay deduped.
.venv\Scripts\python scripts\smoke_e2e.py
```

The smoke test asserts the plan's acceptance criteria end to end, including
the two properties that matter most for a payment gateway: **a replayed
event can never double-credit**, and **a down webhook endpoint still
receives its delivery**.

---

## Security notes

- Webhook secrets are generated server-side, returned once, and verified
  with constant-time comparison.
- The internal ingest endpoint is guarded by `X-Internal-Key` and rejects
  unauthenticated requests before body validation.
- Money crosses the API only as exact decimal strings; conversion to integer
  base units happens in one audited module (`app/money.py`) — never floats.
- `.env` is gitignored; never commit provider keys. If a key was ever shared
  in plaintext, rotate it.

---

## Roadmap

- [ ] Sepolia testnet run with real USDC end-to-end
- [ ] Reconciliation job (DB truth vs. chain truth)
- [ ] Reorg handling (`rollback` path in the state machine)
- [ ] Per-invoice HD-derived addresses (removes amount-offset matching)
- [ ] Multi-chain support (second EVM chain)
- [ ] Split background loops into dedicated workers + queue
- [ ] Merchant dashboard

---

*Local working notes (`plan.md`, `DECISIONS.md`, `REVIEW_FINDINGS.md`) are
intentionally untracked; they guide development but are not part of the
shipped repo.*

