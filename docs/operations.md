# Operations

Running Mpay for real: configuration, process management, and the safety nets.

## Configuration

All settings are environment variables (code: `app/config.py`). Nothing is required for local SQLite dev.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./mpay.db` | Production: `postgresql+psycopg://user:pass@host/db` (install `psycopg[binary]`). Note the `+psycopg` — plain `postgresql://` fails. |
| `RECEIVING_ADDRESS` | zero address | The hot wallet. **Must match the watcher's** or no payment will ever match an invoice. Lowercased automatically. |
| `USDC_CONTRACT` | mainnet USDC | Must be the contract on *your* network (Sepolia: `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`) |
| `CHAIN_ID` | `1` | EVM chain id, encoded into checkout QR payment URIs. **Wrong value = wallets scan to the wrong network.** Sepolia: `11155111` |
| `CONFIRMATION_THRESHOLD` | `12` | Blocks of depth before `CONFIRMING → CONFIRMED`. Sepolia with 12s blocks: 3 is reasonable. |
| `INVOICE_TTL_MINUTES` | `60` | Unpaid invoices expire. Expiry only affects unpaid invoices — a detected payment never expires. |
| `RPC_URL` | *(empty)* | Used by the confirmation sweep and reconciliation cross-check. Empty = offline mode (tests). |
| `INTERNAL_API_KEY` | `dev-internal-key` | Watcher↔API shared secret. Always set a real random value in production. |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | Retry budget per delivery |
| `WEBHOOK_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | `2` / `60` | Exponential backoff shape |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | Per-attempt HTTP timeout |
| `WEBHOOK_ALLOW_PRIVATE_HOSTS` | `false` | SSRF-guard escape hatch. **Dev rigs only** — it exists so the mock webhook receiver can run on loopback. |
| `REORG_SAFETY_DEPTH` | `60` | Payments vanishing within this many blocks of head are rolled back automatically; older anomalies are logged for operators instead. |
| `PROCESSOR_POLL_SECONDS` / `SWEEP_POLL_SECONDS` / `DISPATCHER_POLL_SECONDS` / `RECONCILIATION_POLL_SECONDS` | `1` / `2` / `1` / `30` | Background loop cadences |
| `RECONCILIATION_STALE_PENDING_SECONDS` | `300` | Events pending this long => processor loop suspected dead (alerted, not self-healed) |
| `RECONCILIATION_CROSSCHECK_BATCH` | `25` | Payments re-verified against the chain per pass |

## Watcher configuration (env vars read by the Go binary)

| Variable | Default | Notes |
|---|---|---|
| `RPC_URL`, `API_URL`, `RECEIVING_ADDRESS`, `USDC_CONTRACT`, `INTERNAL_API_KEY` | — | Must match the API's values |
| `CONFIRMATIONS` | `12` | The watcher scans `CONFIRMATIONS` blocks behind head |
| `POLL_SECONDS` | `12` | Note: different name from the Python side's cadence vars |
| `MAX_BLOCKS` | `2000` | Block range per `eth_getLogs` call. **Alchemy free tier: set 10.** Long catch-up ranges are chunked automatically. |
| `STATE_FILE` | `state.json` | Checkpoint. Written atomically; advances only after successful ingest — crashes resume exactly-once. |

## Running as services

Mpay is two long-running processes: the API (which includes the processor, sweep, dispatcher, and reconciliation loops) and the watcher. For production:

- Run both under a supervisor — `systemd` on Linux, `NSSM`/Task Scheduler on Windows, or your existing panel (DeployDock works).
- The watcher's checkpoint file and the API's SQLite file (if used) must live on persistent paths.
- Back up the database; it *is* your ledger. If you use SQLite, stop the API or use SQLite's backup API before copying.
- Point `webhook_url`-able merchants at publicly reachable HTTPS endpoints.

## What happens when things go wrong

| Symptom | Automatic response |
|---|---|
| Watcher down / crash | Restarts behind head, rescans from checkpoint in chunks, replays dedupe at the DB |
| Webhook endpoint down | 5 attempts with backoff, then `failed` — reconciler re-enqueues while the invoice sits `CONFIRMED` |
| Processor loop dead | Stale pending events alert in logs after 5 min |
| Payment reorged off-chain | Invoice rolls back to `AWAITING_PAYMENT`, audit rows marked orphaned, merchant gets `invoice.reorgged` |
| Payment missing beyond reorg window | Logged `ERROR` for operator review — never auto-touched |
| Database vs chain disagreement | Reconciliation cross-check catches it; RPC failure aborts the pass rather than acting on partial data |

## SSRF guard

Merchant-supplied webhook URLs are resolved at delivery time; private, loopback, link-local, and reserved ranges fail terminally. Known limitation: a DNS-rebinding race between check and request is out of scope for v1. Dev rigs that need loopback webhooks set `WEBHOOK_ALLOW_PRIVATE_HOSTS=true`.

## Known v1 boundaries

- Single hot address + unique-amount matching (HD per-invoice addresses are on the roadmap)
- Single token (USDC), single chain
- No refund flow; over/underpayment policy is on the roadmap
- Merchant secrets are shown once and never rotated via API yet
- Webhook secrets are stored as hashes for API keys but plaintext for webhook secrets (HMAC needs the raw key) — treat the DB as sensitive
