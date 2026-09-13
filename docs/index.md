# Mpay Documentation

Mpay is a self-hostable crypto payment engine: it matches on-chain USDC transfers to invoices exactly-once, advances them through a confirmation-aware state machine, and notifies merchants via cryptographically signed webhooks. Funds move payer-to-merchant directly on-chain — Mpay is the verification and notification layer, never a custodian.

## Guides

| Document | Read it to... |
|---|---|
| [Quickstart](quickstart.md) | Go from `git clone` to a live invoice in ~10 minutes |
| [API reference](api.md) | Integrate invoice creation into your backend |
| [Checkout & payment links](checkout.md) | Understand what your payers see and how they pay |
| [Webhooks](webhooks.md) | Receive, verify, and debug payment notifications |
| [Operations](operations.md) | Run Mpay reliably: config, services, reorgs, recovery |
| [Brand](brand.md) | Logo, color, and typography specs |

## How a payment flows

```
You create an invoice          the watcher sees the transfer        you get told
─────────────────────          ─────────────────────────────        ────────────
POST /merchants/{id}/invoices  Go watcher → eth_getLogs             webhook:
  → unique payable amount      → raw event ingested                 invoice.confirmed
  → /pay/{public_id} link      → matched exactly-once               (HMAC-SHA256 signed,
                               → confirmation depth tracked          retried with backoff)
                               → invoice: AWAITING → CONFIRMING
                                 → CONFIRMED → SETTLED
```

## Design principles

1. **Exactly-once is structural, not filtered.** Dedup is a database-level `UNIQUE (tx_hash, log_index)` on both raw events and credited payments — a replayed event is unrepresentable, not merely ignored.
2. **"Paid" is a state machine, not a boolean.** Invoices pass through `AWAITING_PAYMENT → DETECTED → CONFIRMING → CONFIRMED → SETTLED`, with expiry and reorg rollbacks as first-class paths.
3. **Non-custodial.** Payments move wallet-to-wallet on-chain. Mpay holds nothing, so there is nothing to steal from it.
4. **Reconciliation over optimism.** A periodic job re-verifies credited payments against the chain, rescues stuck notifications, and rolls back reorged payments — the database must never quietly disagree with the chain.
5. **Money is integer base units.** USDC has 6 decimals; amounts cross the API as exact decimal strings and live as integers internally. Floats never touch money.
