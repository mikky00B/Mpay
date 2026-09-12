# Mpay — Engineering Decision Log & Progress Tracker

> This file is the project's living log: every architectural decision made while
> building is recorded here with its rationale, alternatives considered, and
> consequences. Progress against `plan.md`'s build order is tracked at the top.
>
> Format: `[D#]` = decision entries, `[P#]` = progress entries. Appended chronologically.

---

## Progress Tracker

| # | Date | Build-order step | Status | Notes |
|---|------|------------------|--------|-------|
| P1 | 2026-09-11 | 0. Scaffold | ✅ | git init, `.gitignore`, venv, `requirements.txt`, this log |
| P2 | 2026-09-11 | 1. DB schema | ✅ | 5 tables verified; duplicate (tx_hash, log_index) rejected at DB level |
| P3 | 2026-09-11 | 2. Invoice API | 🔵 | In progress — state machine + processor already in |
| P4 | 2026-09-11 | 3. Chain watcher (Go) | ⬜ | Not started |
| P5 | 2026-09-11 | 4. Event processor | ⬜ | Not started |
| P6 | 2026-09-11 | 5. Confirmation counter | ⬜ | Not started |
| P7 | 2026-09-11 | 6. Webhook dispatcher | ⬜ | Not started |
| P8 | 2026-09-11 | 7. Reconciliation job | ⬜ | Not started (stretch for now) |

---

## Decisions

### [D1] Project is greenfield — scaffold from scratch
**Date:** 2026-09-11
The workspace contained only `plan.md`. No existing code, no git repo, no lockfile.
**Decision:** `git init` a fresh repo, create a Python 3.11 venv (`.venv/`), and
establish the layout defined in [D2]. Environment audit found Python 3.11.14,
Go 1.26.3, git 2.37, Node 24 — but **no Docker**.

**Decision:** `git init` a fresh repo, create a Python 3.11 venv (`.venv/`),
pin dependencies in `requirements.txt`, and establish the layout in [D2].
Note: the first pip install timed out at the shell level (30s command limit);
it was relaunched as a detached background process and completed after.

### [D2] Repository layout: `app/` (Python API) + `watcher/` (Go) + `tests/`
**Date:** 2026-09-11
plan.md mandates a polyglot stack: FastAPI + PostgreSQL for the API, Go for the
chain watcher. Both services share one database (the Go watcher writes raw
`ChainEvent` rows; the Python API reads/processes them).
**Decision:** top-level packages:
- `app/` — FastAPI application (config, db, models, state machine, event
  processor, webhook dispatcher, routes)
- `watcher/` — Go module, single deployable binary
- `tests/` — pytest suite for the Python side
**Alternatives:** (a) separate `api/` dir — rejected, `app/` is the conventional
FastAPI layout; (b) watcher as a Python asyncio task inside the API process —
rejected: plan.md explicitly wants Go for the watcher, and a separate process
survives API restarts (the watcher keeps writing events while the API is down).

### [D3] SQLite for local dev, PostgreSQL for prod — via a single env var
**Date:** 2026-09-11
Docker is **not available** in this environment, so a local Postgres container is
not an option right now. plan.md targets PostgreSQL.
**Decision:** SQLAlchemy 2.0 with a `DATABASE_URL` setting defaulting to
`sqlite:///./mpay.db` for local dev/tests; switch to
`postgresql+psycopg://...` in production by env var only. Code stays
dialect-agnostic (no SQLite-isms, no Postgres-only types in v1 — JSON payloads
stored as TEXT, portability over cleverness).
**Rationale:** lets every component run and be tested end-to-end on this machine
today, with a zero-code-change path to Postgres.
**Consequence:** dialect-specific features must be avoided; the unique index on
`(tx_hash, log_index)` [D7] is plain and works on both dialects. Revisit
Postgres-native `JSONB`/`NUMERIC` when Docker lands.

### [D4] Money as integer minor units (bigint), never floats
**Date:** 2026-09-11
USDC has 6 decimals. Float arithmetic on money is the classic payments bug.
**Decision:** amounts stored as `bigint` of **base units** (1 USDC = 1_000_000).
API accepts decimal strings like `"25.50"` and converts exactly to `25500000`
via the `decimal` module.
**Consequence:** idempotency math (sum of payments vs invoice amount) is exact
integer comparison — see [D6] on the amount-matching strategy.

### [D5] Watcher → API communication: internal HTTP ingest endpoint
**Date:** 2026-09-11
The Go watcher must get raw on-chain events into the shared DB. Options:
(a) watcher opens its own direct DB connection; (b) watcher POSTs to an internal
API endpoint which writes the `ChainEvent` row.
**Decision:** (b) — internal ingest endpoint `POST /internal/events` guarded by
an `INTERNAL_API_KEY`, writing rows marked `processed=false`. The **event
processor** (build step 4) is the only component that mutates invoice state.
**Rationale:** a single writer path keeps the state machine airtight; the API can
rate-limit/dedupe ingest; the watcher stays a dumb, stateless sensor — easier to
reason about, and matches the "write raw event first, process idempotently later"
split in plan.md.
**Trade-off:** adds an HTTP hop; acceptable at v1 scale, and it means the watcher
needs zero DB drivers/config.

### [D6] v1 address strategy: single shared hot address + unique amounts
**Date:** 2026-09-11
plan.md allows HD per-invoice derivation *or* amount-matching on a shared address.
HD derivation requires custody-grade seed handling — out of scope for v1.
**Decision:** one shared USDC receiving address from env config; each invoice
gets a **unique required amount** (base units + a unique minor-unit offset) so an
incoming USDC `Transfer` is matched to exactly one invoice by `(to, amount)`.
**Consequence:** `Payment` rows record the matched invoice; amount collisions are
impossible while amounts are unique. HD derivation is the v2 upgrade path.

### [D7] Event processor idempotency: unique index + atomic claim, not just checks
**Date:** 2026-09-11
Acceptance criterion: replaying the same chain event twice must not double-credit.
A checks-then-insert pattern has a TOCTOU race under concurrency.
**Decision:** `chain_events` has a **unique index on `(tx_hash, log_index)`**, so
the same on-chain event can be recorded only once (duplicate ingest becomes a
DB-level no-op); processing is guarded by a `status` column
(`pending → processed | skipped`) flipped inside the same transaction that
applies the state transition. Dedup happens at the DB layer, not the code layer.

### [D8] Webhook delivery: attempt log + backoff, HMAC-SHA256 signatures
**Date:** 2026-09-11
**Decision:** every attempt appends a `webhook_deliveries` row (status, response
code, latency, error). A dispatcher loop retries failed deliveries with
exponential backoff (2s base, doubling, capped at 60s), max 5 attempts, then
marks the delivery `failed`. Payload is HMAC-SHA256 signed over the raw body,
keyed by the merchant secret, sent as `X-Mpay-Signature`. In v1 the dispatcher
runs as an asyncio background task inside the API process; extract to a separate
worker if load ever demands it.
**Rationale:** the delivery log is a plan.md requirement and is what makes
webhook debugging actually possible in production.

### [D9] Verification strategy: behavioral tests against a real DB
**Date:** 2026-09-11
**Decision:** tests exercise the real state machine, idempotency, and webhook
signing against a temp-file SQLite DB — not mocks — because the acceptance
criteria are behavioral ("replay must not double-credit", "down webhook gets
retried"). Outbound webhook HTTP is intercepted at the transport layer, so
dispatcher logic (backoff, signing, retry bookkeeping) runs for real while the
network is simulated.

### [D10] Datetimes stored as naive UTC everywhere
**Date:** 2026-09-11
SQLite drops `tzinfo` on write; mixing timezone-aware objects with
`DateTime(timezone=True)` columns produces inconsistent reads across dialects.
**Decision:** all model timestamps are **naive UTC** (`utcnow()` strips tzinfo
after computing in UTC); comparisons and expiry math operate in UTC consistently
on SQLite and Postgres alike. Serialization to API clients uses explicit
ISO-8601 `Z` suffix formatting. Classic pitfall avoided by convention at the
model layer rather than by dialect-specific column types.

### [D11] State machine transitions centralized in one module, guard-railed
**Date:** 2026-09-11
**Decision:** `app/state_machine.py` exposes `transition(invoice, target)` — the
ONLY place invoice status changes. It validates moves against a legal-transition
table (illegal moves raise `IllegalTransition`), turning "status drifted out of
sync" bug classes into loud errors. Transitions carrying side effects (merchant
notification) are separate concerns, applied by their owning component
(processor / confirmation counter / dispatcher), never inline.
**Rationale:** plan.md's core pitch is a real state machine; encoding legal
moves in one table makes the lifecycle auditable and testable in isolation.

### [D12] E2E smoke test as a real-process orchestration script
**Date:** 2026-09-11
**Decision:** `scripts/smoke_e2e.py` + `scripts/mocks.py` run the ACTUAL system:
uvicorn API (with live background loops), the compiled Go watcher binary, a
mock JSON-RPC chain (:8555) and an HMAC-verifying webhook receiver (:9801). It
asserts the full journey AWAITING_PAYMENT → CONFIRMED → SETTLED, a 503'd
webhook being retried and signed correctly, and replay-dedupe (no double
credit). Artifacts (`smoke.db`, `smoke_state.json`, logs) are gitignored.
**Rationale:** unit tests prove pieces; the smoke proves the system. Several
integration-only bugs surfaced here that unit tests could not catch: API env
missing RECEIVING_ADDRESS (invoices pointed at the null address), the mock
chain only advancing height on getLogs (self-deadlocking), fake addresses
failing hex validation in the watcher, and a `create_all` that silently
created nothing without importing models first.

### [D13] Watcher checkpoint starts at head, never rescans history
**Date:** 2026-09-11
**Decision:** on first run the watcher checkpoints `eth_blockNumber` (head)
and only scans blocks discovered afterwards (staying `CONFIRMATIONS` behind
head to respect the confirmations model). Checkpoint persists atomically
(tmp+rename) and advances only after successful ingest, so crashes/retries
are always safe.
**Rationale:** rescanning full history on cold start is slow and pointless for
new merchants; "start at now" is the operationally sane default. The smoke
test initially placed its fake transfer in the PAST which the (correct)
watcher never saw — test data had to move to a future block instead.

### [D14] Tests pin every behavior-relevant setting via monkeypatch
**Date:** 2026-09-11
**Decision:** `tests/conftest.py` explicitly sets CONFIRMATION_THRESHOLD and
INVOICE_TTL_MINUTES (and all other behavior-relevant env vars) for every test.
**Rationale:** pydantic-settings reads the developer's real `.env` file too —
a locally-set `CONFIRMATION_THRESHOLD=3` silently changed sweep behavior and
failed a test that assumed the default of 12. Test assertions must be
deterministic regardless of machine-local configuration, so tests pin what
they assert on rather than trusting defaults.

---

## Progress Log

### 2026-09-11 — v1 backend COMPLETE, all acceptance criteria demonstrated
- ✅ DB schema: 5 entities (merchants, invoices, chain_events, payments,
  webhook_deliveries) with UNIQUE (tx_hash, log_index) [D7]
- ✅ Invoice API: merchants, idempotent invoice creation, status/paid queries,
  delivery log endpoint
- ✅ Go chain watcher: eth_getLogs → POST /internal/events, checkpointed,
  crash-safe, resumes exactly-once [D13]
- ✅ Idempotent event processor: single writer to invoice state [D5]
- ✅ Confirmation counter: CONFIRMING → CONFIRMED at depth ≥ threshold
- ✅ Webhook dispatcher: HMAC-SHA256 signed, exponential backoff, retry log
- ✅ **18/18 unit/behavior tests green** (`pytest -q`)
- ✅ **End-to-end smoke PASSED** [D12]: real uvicorn + real Go watcher + mock
  chain + HMAC-verifying webhook receiver; AWAITING_PAYMENT → CONFIRMED →
  SETTLED; 503-retry delivered; replay dedupe held (duplicates=1, no double
  credit)
- ⏳ Remaining (stretch per plan.md): Sepolia testnet run with real USDC
  contract (`RPC_PROVIDER=alchemy` config already in `.env`), reconciliation
  job, reorg handling, multi-chain, dashboard.

### 2026-09-12 — Review findings fixed: [D15] + [D16]
- ✅ **[D15]** Finding 1 (high): payable offset `id % 900 + 1` wrapped every
  900 invoices — two simultaneously open invoices with the same requested
  amount could share a (address, amount) matching key and a payment could be
  credited to the wrong one. Fixed three ways:
  (a) offset is now the invoice id itself — uniqueness holds forever;
  (b) partial UNIQUE index `uq_invoices_open_address_amount` on
  `(receiving_address, amount_base_units) WHERE status = 'AWAITING_PAYMENT'`
  (dialect-safe: sqlite_where + postgresql_where) so any residual collision
  fails loudly with 409 at creation instead of corrupting matching silently.
  Note: the guarded flush must happen AFTER the CREATED -> AWAITING_PAYMENT
  transition, because the index only evaluates rows in that status;
  (c) `_find_matching_invoice` now has deterministic `.order_by(Invoice.id)`.
  Trade-off documented: offset grows unboundedly with id (rounding-level at
  6 decimals); HD per-invoice addresses remain the future escape hatch.
- ✅ **[D16]** Finding 2 (medium): Go watcher wedged forever against real
  providers. (a) chunked eth_getLogs scanning — MAX_BLOCKS window (default
  2000, env-overridable), checkpoint advances per window; a failed window is
  retried next tick with progress kept; (b) chunked ingest — batches of <=500
  to respect the API's IngestBatch limit, checkpoint only after ALL batches
  accepted; (c) hexToUint64 and currentHeight now return errors instead of
  silently yielding 0 on malformed RPC responses.
- ✅ Fixed a latent test bug found in the process: conftest's
  Base.metadata.create_all ran before app.models metadata was loaded —
  worked only because other modules imported it transitively; running
  test_api.py alone created an empty DB ("no such table: merchants").
- ✅ Smoke env: API process now pins INTERNAL_API_KEY (the developer's .env
  value leaked in and 401'd the watcher's ingest — same class of bug as [D14]).
- ✅ 4 regression tests added (ids 900 apart, never-wraps sweep, duplicate
  open (address, amount) -> 409, settled amount reusable). **21/21 pass**;
  watcher rebuilt (go vet clean); **E2E smoke PASSED** with fixes live.


---

