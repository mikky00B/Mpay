# API Reference

Base URL: `http://your-host:8000`. All money values are exact decimal strings of USDC (6 decimals); internal amounts are integer base units. An interactive OpenAPI explorer ships with the service at `/api-docs`.

> These pages are also served live by every Mpay instance at **`/docs`** — sidebar navigation, full-text search, dark mode — so integrators always have docs matching the running version.

Merchant-scoped endpoints require the `X-API-Key: mpay_sk_…` header issued at merchant creation. Public endpoints (`/invoices/{public_id}`, `/pay/{public_id}`, `/health`) are intentionally keyless — `public_id` is a 16-byte random capability.

---

## POST /merchants

Create a merchant. No auth (onboarding).

**Body**

```json
{"name": "My Shop", "webhook_url": "https://your-app.example/hooks/mpay"}
```

`webhook_url` must be publicly reachable — private/loopback targets are rejected with `422` ([SSRF guard](operations.md#ssrf-guard)). It can be empty and set later.

**Response `201`**

```json
{
  "id": 1,
  "name": "My Shop",
  "webhook_url": "https://your-app.example/hooks/mpay",
  "webhook_secret": "whsec_3d401c…",
  "api_key": "mpay_sk_81f570…"
}
```

Both secrets are shown **once** and stored only as hashes. There is no rotation endpoint yet; rotating = creating a new merchant.

---

## POST /merchants/{merchant_id}/invoices

Create an invoice. Requires `X-API-Key` for the owning merchant.

**Body**

```json
{"amount": "25.50", "description": "Order #42", "idempotency_key": "order-42"}
```

| Field | Rules |
|---|---|
| `amount` | Decimal string, > 0, max 6 decimals (`422` otherwise) |
| `idempotency_key` | Optional, ≤ 120 chars, unique per merchant. Repeating a call with the same key returns the **original invoice** with `"idempotent_replay": true` instead of creating a duplicate |

**Response `201`**

```json
{
  "invoice": {
    "public_id": "2033f089…",
    "status": "AWAITING_PAYMENT",
    "requested_amount": "25.5",
    "payable_amount": "25.500042",
    "paid_amount": "0",
    "receiving_address": "0xd6ba…",
    "detected_block": null,
    "confirmed_block": null,
    "expires_at": "2026-09-13T15:12:06Z",
    "created_at": "2026-09-13T14:42:06Z"
  },
  "idempotent_replay": false
}
```

**Key semantics**

- **`payable_amount` ≠ `requested_amount`.** The payable amount is the requested amount plus a tiny per-invoice unique offset. It is the matching key: an incoming transfer is credited to the open invoice whose payable amount it equals exactly. Send `payable_amount` to payers, never `requested_amount`.
- **`409`** is returned if another *open* invoice already claims the same `(receiving_address, payable_amount)` pair — settled invoices release their amount for reuse.
- Invoices expire after `INVOICE_TTL_MINUTES` (default 60) if unpaid: status → `EXPIRED` (terminal).
- **Errors:** `401` missing/invalid key · `404` unknown merchant · `409` amount collision · `422` invalid amount.

**Invoice statuses**

`AWAITING_PAYMENT` → `DETECTED` → `CONFIRMING` → `CONFIRMED` → `SETTLED`, with `EXPIRED` (unpaid past TTL) as a terminal side path and rollback to `AWAITING_PAYMENT` if a confirmed payment is reorged off the chain.

---

## GET /invoices/{public_id}

Public. Full invoice state plus credited payments — this is the endpoint the checkout page polls.

```json
{
  "invoice": { "…": "same shape as above" },
  "payments": [
    {"tx_hash": "0xb8bb…", "log_index": 12, "block_number": 11696557, "amount": "3.500006"}
  ]
}
```

---

## GET /pay/{public_id}

Public, HTML. The hosted checkout page: QR (EIP-681, chain- and amount-qualified), copyable receiving address, live status. See [Checkout](checkout.md).

---

## GET /merchants/{merchant_id}/invoices

Requires `X-API-Key`. Most recent 200 invoices, newest first.

---

## GET /merchants/{merchant_id}/deliveries

Requires `X-API-Key`. The webhook delivery log — one row per HTTP attempt, with status, response code, error text, and next-retry time. This is the debugging surface when a merchant says "I wasn't notified."

---

## POST /internal/events

Watcher-only ingest, guarded by the `X-Internal-Key` header (a separate trust domain from merchant API keys). Idempotent on `(tx_hash, log_index)`; replays return `{"duplicates": n}`. You normally never call this — the Go watcher does.
