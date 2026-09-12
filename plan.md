# Merchant Payment Gateway (Blockchain Payments)

## 1. Overview

A backend-focused payment gateway that lets merchants accept crypto payments (starting with USDC on Ethereum/EVM) the way Stripe or Coinbase Commerce does: create an invoice, receive payment on-chain, confirm it reliably, and notify the merchant via webhook.

The point of this project is not "talk to a blockchain" — that part is a few API calls. The point is building the same reliability primitives that real payments infrastructure needs: idempotency, state machines, confirmation/finality handling, and safe webhook delivery. This is what makes it a strong technical portfolio piece and a strong interview talking point.

**Scope decision:** Backend-only. No smart contracts, no Solidity. Consume an existing chain via an RPC provider (Alchemy or Infura) or a client library (web3.py).

**Stack:**
- API: FastAPI + PostgreSQL
- Chain watcher: Go 
- Chain access: web3.py / Alchemy or Infura REST + webhooks
- Webhooks out: HMAC-signed payloads with retry/backoff

---

## 2. Core Concepts

### Entities
- **Merchant** — account that owns invoices, has a webhook URL + secret
- **Invoice** — a payment request: amount, token, receiving address, status
- **ChainEvent** — raw, unprocessed record of something seen on-chain (a transfer to a watched address). Written first, processed idempotently after.
- **Payment** — a processed, matched payment against an invoice, derived from one or more ChainEvents
- **WebhookDelivery** — log of an attempt to notify the merchant, with retry state

### Invoice State Machine
```
CREATED
  → AWAITING_PAYMENT
    → DETECTED        (tx seen, 0 confirmations)
      → CONFIRMING     (1..N confirmations, N = configurable threshold)
        → CONFIRMED    (N confirmations reached)
          → SETTLED    (merchant notified successfully)
    → EXPIRED          (timeout with no payment seen)
```

Why a state machine and not a boolean `paid`: a transaction can sit unconfirmed, get replaced, or (rarely) be part of an orphaned block. Treating "paid" as binary is the naive version of this system; treating it as a state machine with a rollback path is the version that actually reflects how payments infra works.

---

## 3. v1 Scope (build this first, get it working end-to-end)

1. **Chain support:** Ethereum/EVM only, single token (USDC). No multi-chain yet.
2. **Invoice creation endpoint** — generates a receiving address per invoice (HD wallet derivation), or alternatively matches by amount on a single shared address if derivation is out of scope initially.
3. **Chain watcher** — polls the RPC provider every N seconds (or subscribes to pending/mined tx events) and writes raw events into `ChainEvent`, unprocessed. This separation (write raw event first, process later) is what makes reprocessing/idempotency possible.
4. **Event processor** — idempotent, keyed on `(tx_hash, log_index)` so the same on-chain event can never be double-applied. Moves the invoice through the state machine.
5. **Confirmation counter** — on each new block, re-checks depth for invoices in `CONFIRMING` and flips to `CONFIRMED` once the threshold is hit.
6. **Webhook dispatcher** — HMAC-signed payload (so merchants can verify authenticity), retry with exponential backoff, full delivery log for debugging.

### v1 acceptance criteria
- A test invoice created via API can be paid on a testnet (e.g. Sepolia) and reach `SETTLED` end-to-end with no manual intervention.
- Replaying the same chain event twice does not double-credit an invoice.
- A merchant webhook endpoint that's down gets retried and eventually delivered (or logged as failed after max retries).

---

## 4. Stretch Goals (after v1 works)

- **Reorg handling** — roll back a `CONFIRMED` invoice if its block gets orphaned. Rare in practice but a strong "I actually thought about this" interview point.
- **Overpayment / underpayment handling** — define and implement the policy (partial credit? refund? require exact match?).
- **Second token or chain** — proves the abstraction (ChainEvent, state machine) actually generalizes rather than being hardcoded to USDC/Ethereum.
- **Merchant dashboard** — React frontend showing invoice status, payment history, webhook delivery logs.

---

## 5. Why This Project Is Interview-Relevant

| Concept | Where it shows up |
|---|---|
| Idempotency | Event processor keyed on tx_hash + log_index |
| Race conditions | Watcher and processor running concurrently on same events |
| Eventual consistency | Reconciliation job cross-checking DB vs chain state |
| State machines | Invoice lifecycle |
| Finality / confirmation depth | CONFIRMING → CONFIRMED transition |
| Reliable delivery | Webhook retry/backoff + delivery log |
| Security | HMAC-signed webhook payloads |

This maps directly onto the kind of system-design and backend-reliability questions that come up in real interviews — the project itself becomes your answer to "tell me about a time you handled a hard concurrency/reliability problem."

---

## 6. Build Order

1. DB schema: Merchant, Invoice, ChainEvent, Payment, WebhookDelivery
2. Invoice creation API + address generation
3. Chain watcher (write-only, dumps raw events)
4. Event processor (idempotent state machine transitions)
5. Confirmation counter job
6. Webhook dispatcher + retry logic
7. Reconciliation job (sweep DB vs chain, catch anything missed)
8. Stretch goals as time allows