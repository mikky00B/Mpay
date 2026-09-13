# Webhooks

Mpay notifies your backend the moment an invoice reaches a state you care about. Notifications are HMAC-signed, retried with exponential backoff, and every attempt is logged.

## Events

| Event | Meaning |
|---|---|
| `invoice.confirmed` | The payment reached `CONFIRMATION_THRESHOLD` blocks of depth — safe to fulfill |
| `invoice.settled` | The `invoice.confirmed` notification was delivered successfully (invoice terminal) |
| `invoice.expired` | The invoice TTL elapsed with no payment |
| `invoice.reorgged` | A previously confirmed payment was reorged off the chain; the invoice rolled back to `AWAITING_PAYMENT`. If you already fulfilled the order, this is your signal to review |

More event types may be added; treat unknown `type` values as non-fatal.

## Payload

JSON, `POST`, `Content-Type: application/json`, one attempt per row in the delivery log:

```json
{
  "type": "invoice.confirmed",
  "created_at": "2026-09-13T14:55:03.123456Z",
  "data": {
    "invoice_id": "2033f089…",
    "merchant_id": 6,
    "status": "CONFIRMED",
    "requested_base_units": 3500000,
    "amount_base_units": 3500006,
    "paid_base_units": 3500006,
    "token": "USDC",
    "chain": "ethereum",
    "receiving_address": "0xd6ba…"
  }
}
```

Money fields are integer base units (1 USDC = 1,000,000). `status` is the invoice's status **at enqueue time**; poll `GET /invoices/{public_id}` for current truth.

## Verifying signatures

Every request carries `X-Mpay-Signature`: the HMAC-SHA256 hex digest of the **raw request body**, keyed by your merchant's `webhook_secret`. Verify over the exact bytes received — before JSON parsing:

```python
import hmac, hashlib

def verify(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    expected = hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Always compare in constant time (`compare_digest`, not `==`). If verification fails, return a non-2xx status — Mpay will retry.

## Delivery & retry semantics

- One attempt = one row in `GET /merchants/{id}/deliveries`, with response code, error, and next-retry time.
- **Success** = any 2xx. Anything else (non-2xx, timeout, connection error) schedules a retry with exponential backoff: 2s, 4s, 8s, 16s, 32s (capped at 60s), up to `WEBHOOK_MAX_ATTEMPTS` (default 5).
- After the final failed attempt the delivery is marked `failed` and **stops retrying** — unless a delivery is needed for an invoice sitting in `CONFIRMED`, in which case the reconciliation job re-enqueues it (indefinitely, capped per sweep): real money arrived, so notification must eventually succeed.
- A webhook URL pointing at a private/loopback address fails terminally at delivery time (SSRF guard) — reconfigure the merchant's URL.

## Debugging "I wasn't notified"

1. `GET /merchants/{id}/deliveries` (with your API key) — read the latest rows: `pending` (backoff not elapsed), `failed` + error text, or nothing at all.
2. Check the invoice itself via `GET /invoices/{public_id}` — no webhook is sent before `CONFIRMED`.
3. `invoice.confirmed` delivered but never `invoice.settled`? That means your endpoint never returned a 2xx for the first one.

## Best practices

- Respond **2xx immediately**, process asynchronously. Slow handlers burn your retry budget.
- Treat webhooks as hints, not state: the invoice endpoint is the source of truth.
- Deduplicate by `invoice_id` + `type` — at-least-once delivery means repeats are possible.
