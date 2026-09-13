# Checkout & Payment Links

Every invoice has a hosted checkout page — the payment link you send to customers:

```
https://your-host/pay/{public_id}
```

No integration is required to *use* it; it works from a phone's camera or a desktop browser.

## What the payer sees

1. **Amount, token, and network** — the payable amount is shown large and exact.
2. **QR code** — encodes an [EIP-681](https://eips.ethereum.org/EIPS/eip-681) payment URI:

   ```
   ethereum:<USDC contract>@<chainId>/transfer?address=<receiving address>&uint256=<base units>
   ```

   Wallet apps that scan it pre-fill the **token contract**, the **network**, and the **exact amount**. The `@chainId` qualifier matters: without it, the URI defaults to Ethereum mainnet — a QR generated for Sepolia would silently point a wallet at the wrong network. Mpay encodes `CHAIN_ID` from settings; verify it matches your deployment.
3. **Open in wallet app** button — the same EIP-681 URI as a deep link for mobile.
4. **Copyable receiving address** — for manual sends. The payer must send the **exact payable amount**; the unique per-invoice amount is the matching key, and a wrong amount will never be credited.
5. **Live status stepper** — Waiting → Detected → Confirming → Confirmed, polling every few seconds. The page needs no refresh; it updates itself through the invoice lifecycle and shows a clear terminal state for `SETTLED` and `EXPIRED`.

## The matching model (why amounts are unique)

All invoices share one receiving address, so the amount is what distinguishes them. Mpay adds a tiny per-invoice offset to the requested amount, guaranteeing every *open* invoice asks for a distinct amount. Consequences worth knowing:

- A transfer that doesn't equal an open invoice's payable amount is recorded but **never credited** (and flagged by reconciliation if it looks like a near-miss).
- Once an invoice settles, its payable amount is released for reuse.
- If two open invoices would collide on the same payable amount, creation fails loudly (`409`) rather than corrupting matching.

The alternative design — a unique derived address per invoice (HD wallet) — is on the roadmap and removes the exact-amount requirement, at the cost of key custody.

## Status lifecycle

```
AWAITING_PAYMENT → DETECTED → CONFIRMING → CONFIRMED → SETTLED
        │                                       
        └─ (TTL expires) → EXPIRED              
```

- `DETECTED`: the transfer is seen on-chain (0 confirmations)
- `CONFIRMING`: waiting for `CONFIRMATION_THRESHOLD` blocks of depth
- `CONFIRMED`: depth reached — trustworthy
- `SETTLED`: the merchant's webhook was delivered (money + notification both done)
- If a confirmed payment is later reorged off the chain, the invoice rolls back to `AWAITING_PAYMENT` and the merchant receives an `invoice.reorgged` webhook.

## Embedding / branding

The page is a single Jinja2 template (`app/templates/checkout.html`) with inline CSS and ~40 lines of JS — no external assets, no build step, dark-mode aware. Customize it freely for your own branding; the only contract is that it polls `GET /invoices/{public_id}` for status. See [Brand](brand.md) for the palette and typography Mpay ships with.
