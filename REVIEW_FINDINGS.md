# Review findings — please fix before next commit

> Found in a code review on 2026-09-12. Everything else in v1 was verified working:
> 18/18 tests pass, the E2E smoke passes, idempotency/state machine/webhook logic
> check out. Only the two defects below need code changes. Please append a [D15]/[D16]
> entry to DECISIONS.md when each is fixed.

---

## FINDING 1 — Invoice amount offsets collide every 900 invoices (can misdirect payments)

**Severity: high** — this is the one defect that can credit real money to the wrong invoice.

**Where:** `app/money.py` → `unique_offset_for_invoice()`

```python
return invoice_id % 900 + 1
```

**Problem:** the offset repeats every 900 invoice ids. Verified empirically:

```
invoice id 1   -> offset 2
invoice id 901 -> offset 2
ids 1..1800    -> only 900 distinct offsets, all reused
```

Two **simultaneously open** invoices with the same requested amount then share the
same `(receiving_address, amount_base_units)` matching key. `_find_matching_invoice()`
in `app/processor.py` selects with `.limit(1)` and **no ORDER BY**, so an incoming
transfer is credited to an arbitrary one of them — the payer's invoice can stay
AWAITING_PAYMENT until it expires while another invoice gets the money.

This also makes the claim in DECISIONS.md [D6] ("no two open invoices ever ask for
the same payable amount") false as written.

**Suggested fixes (either is fine, doing both is best):**
1. Make the offset truly unique: use `invoice_id` directly as the offset in base
   units instead of `invoice_id % 900 + 1`. Uniqueness then holds forever. Decide
   and document the trade-off (offset grows unboundedly with id, vs. the current
   sub-cent cap) in DECISIONS.md.
2. Defense in depth: add a **partial unique index** on
   `(receiving_address, amount_base_units)` where `status = 'AWAITING_PAYMENT'`
   so a colliding invoice creation fails loudly at insert time instead of
   corrupting matching silently at processing time. (SQLite and Postgres both
   support partial indexes — no dialect conflict with [D3].)
3. While you're there: give `_find_matching_invoice()` a deterministic
   `.order_by(Invoice.id)` so matching behavior is reproducible even if a
   collision ever sneaks in.

**Tests to add:** one that creates invoices whose ids are exactly 900 apart
(you can insert rows with explicit ids) with the same requested amount, and asserts
their payable amounts differ; and one asserting a second invoice with a duplicate
`(address, amount)` is rejected by the new unique index.

---

## FINDING 2 — Go watcher wedges permanently against a real RPC provider

**Severity: medium** — invisible against the mock chain, guaranteed to bite on
Alchemy/Infura after any extended outage.

**Where:** `watcher/types.go` (`filterLogs`) and `watcher/loop.go` (`run`).

**Problem A — unbounded block range:** `filterLogs` fetches `[last+1, target]` in a
single `eth_getLogs` call with no chunking. Real providers cap ranges (~10k blocks
on Alchemy, ~5k default on many Infura tiers). After an outage long enough to fall
that far behind, the provider rejects the range, `getLogs` fails every tick, the
checkpoint never advances, and the watcher is stuck **forever** — it can never
catch up because the failing range only grows.

**Problem B — unbounded batch size:** `loop.go` marshals every log it found into one
ingest POST. The API rejects batches over 500 events (`IngestBatch` max_length=500
in `app/routes.py`) with a 422. A block range containing >500 transfers (busy
period, or catching up after downtime) → 422 → retry forever → same permanent
wedge, even when the getLogs range itself was legal.

**Suggested fixes:**
1. Chunk block scanning: iterate in fixed windows (e.g. 2,000 blocks per
   `eth_getLogs` call), only advancing the checkpoint at window boundaries.
   On a provider "range too large" error, halve the window and retry (adaptive
   backoff), or just use a conservative constant.
2. Chunk ingest: split the event slice into batches of ≤500 before POSTing
   (the API's limit), and only advance the checkpoint after ALL batches of the
   window are accepted. Replays are already safe per [D7], so partial failure +
   resend is fine.
3. Minor: `currentHeight()` in `loop.go` ignores the `json.Unmarshal` error
   (`_ = json.Unmarshal(raw, &s)`) and `hexToUint64()` swallows parse errors —
   a malformed response silently becomes height 0. Return the error instead so
   a bad RPC response is logged rather than misread as "chain at block 0".

**Tests to add:** a mock RPC that returns an error for ranges > N blocks and
assert the watcher splits its scan; a mock ingest that 422s batches > 500 and
assert chunked POSTs; assert the checkpoint only advances after full success.

---

## FINDING 3 — Address case-sensitivity silently drops real payments (found in live Sepolia test)

**Severity: high** — discovered 2026-09-12 when a real on-chain payment was
ingested but SKIPPED instead of credited.

**Where:** `app/routes.py` (invoice creation + ingest) and `app/processor.py`
(matching query).

**Problem:** `.env` holds the EIP-55 checksummed address
(`0xd6ba6754cE076A9922ffD42aDDC771A0E27AF2a8`, mixed case). Invoices store it
as-is. The watcher lowercases every address it reads from chain logs
(`strings.ToLower` in loop.go). SQLite comparison is **case-sensitive**
(binary collation), so `Invoice.receiving_address == event.to_address` is
False for every real payment. The mock chain never caught this because its
HOT test address was all-lowercase.

**Live evidence:** event for real tx 0x75d769b2… (2.000001 USDC, block
11686242) ingested at 04:52 and marked SKIPPED; after normalizing both sides
to lowercase and requeuing, the same event credited and settled correctly.

**Fix:** normalize to lowercase at every boundary —
1. `create_invoice`: store `s.receiving_address.lower()`.
2. `ingest_events`: store `ev.to_address.lower()` (and from/contract for consistency).
3. Keep the processor comparison as a plain `==` (both sides now canonical).
4. Add a regression test with a MIXED-CASE receiving address in the settings
   and a lowercase event, asserting the event credits.

**Note for the fix:** the event was recovered in the live DB by manual
normalization; also worth knowing — SQLAlchemy stores enum NAMES
('PENDING'/'SKIPPED', uppercase) in the status columns, not the enum values
('pending'), which matters for any manual SQL / data repair.

**Free-tier lesson [D16]:** Alchemy's FREE tier caps eth_getLogs at **10
blocks** (not 10k). The chunked scan works but the watcher must be launched
with `MAX_BLOCKS=10` there (start_watcher.bat now sets this). Consider
documenting per-plan values in DECISIONS.md.
