"""Reconciliation job (build step 7) — DB vs chain consistency [D18].

The processor/sweep/dispatcher loops all make forward progress; this job asks
the opposite question: "does the database agree with reality?" Four checks:

1. RESCUE STUCK CONFIRMATIONS — an invoice sitting in CONFIRMED whose
   invoice.confirmed deliveries all FAILED means the merchant was never
   notified. Re-enqueue the webhook (capped per sweep so a long outage can't
   create a delivery storm). Intentional policy [D18]: this retries forever
   at sweep cadence until the webhook succeeds — CONFIRMED means real money
   arrived, so notification must eventually get through.
2. STALE PENDING EVENTS — chain_events stuck 'pending' past a threshold mean
   the processor loop died. ALERT ONLY: the reconciler never applies state
   transitions itself (single-writer rule [D5]).
3. SUSPICIOUS SKIPS — a 'skipped' event whose (to_address, amount) matches a
   currently-open invoice is the exact signature of the live case-sensitivity
   bug [D17]. Make it observable, alert only.
4. CHAIN CROSS-CHECK — re-verify credited payments against the chain via
   eth_getTransactionReceipt. In v1 this REPORTS mismatches (missing receipt
   or changed block hash); acting on them (rollback) is the reorg job [D19].
   Only payments inside the reorg-safety window are actionable; a payment
   that old can only mean an RPC/data problem, not an orphan.

`fetch_receipt` is injectable so tests never touch the network; offline mode
(empty rpc_url) skips the cross-check entirely.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ChainEvent,
    ChainEventStatus,
    DeliveryStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    WebhookDelivery,
    utcnow,
)

log = logging.getLogger(__name__)


class RpcUnavailable(Exception):
    """The RPC endpoint could not be reached — cross-check results are void."""


def fetch_receipt(tx_hash: str, rpc_url: str | None = None) -> dict | None:
    """eth_getTransactionReceipt as a dict, or None if the node answered
    `null` (unknown transaction). Transport failures raise RpcUnavailable so
    callers never confuse "RPC down" with "transaction gone"."""
    url = rpc_url if rpc_url is not None else get_settings().rpc_url
    if not url:
        raise RpcUnavailable("no rpc_url configured (offline mode)")
    import httpx

    try:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                  "params": [tx_hash]},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["result"]
    except Exception as exc:  # transport / parse / node error
        raise RpcUnavailable(str(exc)) from exc


def _rescue_stuck_confirmations(db: Session) -> int:
    """Re-enqueue invoice.confirmed for CONFIRMED invoices never notified."""
    s = get_settings()
    rescued = 0
    invoices = (
        db.execute(select(Invoice).where(Invoice.status == InvoiceStatus.CONFIRMED))
        .scalars()
        .all()
    )
    for inv in invoices:
        if rescued >= s.reconciliation_rescue_cap:
            break
        deliveries = (
            db.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.invoice_id == inv.id,
                    WebhookDelivery.event_type == "invoice.confirmed",
                )
            )
            .scalars()
            .all()
        )
        # No delivery row at all, or every attempt failed -> the merchant has
        # not been told. Any pending/delivered row means it's in flight or done.
        if any(d.status in (DeliveryStatus.PENDING, DeliveryStatus.DELIVERED)
               for d in deliveries):
            continue
        from app.webhooks import enqueue

        enqueue(db, inv, "invoice.confirmed")
        rescued += 1
        log.warning("reconciliation: re-enqueued invoice.confirmed for %s "
                    "(%s prior failed attempts)", inv.public_id, len(deliveries))
    return rescued


def _stale_pending_events(db: Session) -> int:
    s = get_settings()
    cutoff = utcnow() - timedelta(seconds=s.reconciliation_stale_pending_seconds)
    stale = (
        db.execute(
            select(ChainEvent)
            .where(ChainEvent.status == ChainEventStatus.PENDING,
                   ChainEvent.created_at < cutoff)
            .limit(50)
        )
        .scalars()
        .all()
    )
    for ev in stale:
        log.warning("reconciliation: chain_event %s:%s pending since %s — "
                    "processor loop alive?", ev.tx_hash[:16], ev.log_index,
                    ev.created_at)
    return len(stale)


def _suspicious_skips(db: Session) -> int:
    """Skipped events whose (to_address, amount) matches an OPEN invoice."""
    stmt = (
        select(ChainEvent)
        .join(
            Invoice,
            (Invoice.receiving_address == ChainEvent.to_address)
            & (Invoice.amount_base_units == ChainEvent.amount_base_units),
        )
        .where(ChainEvent.status == ChainEventStatus.SKIPPED,
               Invoice.status == InvoiceStatus.AWAITING_PAYMENT)
        .limit(50)
    )
    rows = db.execute(stmt).scalars().all()
    seen: set[int] = set()
    for ev in rows:
        if ev.id in seen:
            continue
        seen.add(ev.id)
        log.warning("reconciliation: skipped event %s:%s (%s base units to %s) "
                    "matches an OPEN invoice — investigate",
                    ev.tx_hash[:16], ev.log_index, ev.amount_base_units,
                    ev.to_address[:12])
    return len(seen)


def _crosscheck_payments(db: Session, latest_block: int | None,
                         fetch) -> dict[str, int]:
    """Re-verify credited payments against the chain. Report-only in [D18]."""
    s = get_settings()
    stats = {"checked": 0, "mismatched": 0}
    if latest_block is None:
        return stats  # offline mode — nothing to verify against
    payments = (
        db.execute(
            select(Payment)
            .order_by(Payment.created_at.desc())
            .limit(s.reconciliation_crosscheck_batch)
        )
        .scalars()
        .all()
    )
    for p in payments:
        try:
            receipt = fetch(p.tx_hash)
        except RpcUnavailable as exc:
            log.warning("reconciliation: cross-check aborted (%s)", exc)
            break  # RPC trouble voids the whole pass — do NOT act on partial data
        stats["checked"] += 1
        ev = db.get(ChainEvent, p.chain_event_id)
        receipt_ok = receipt is not None and receipt.get("blockHash") == ev.block_hash
        if receipt_ok:
            continue
        stats["mismatched"] += 1
        if p.block_number > latest_block - s.reorg_safety_depth:
            # Inside the reorg window — actionable, see [D19] (rollback job).
            log.warning("reconciliation: payment %s:%s (invoice %s) NOT found on "
                        "chain with matching block hash — orphan candidate",
                        p.tx_hash[:16], p.log_index, p.invoice_id)
        else:
            log.error("reconciliation: payment %s:%s (invoice %s) missing on "
                      "chain OUTSIDE the reorg safety window — not actionable "
                      "automatically, requires operator review",
                      p.tx_hash[:16], p.log_index, p.invoice_id)
    return stats


def reconcile(db: Session, latest_block: int | None = None,
              fetch=fetch_receipt) -> dict[str, int]:
    """One reconciliation pass. `latest_block` injects chain height (None =
    offline: height fetch happens in confirmations.get_latest_block fashion at
    the caller, or is skipped in tests). `fetch` is injectable for tests."""
    from app.confirmations import get_latest_block

    if latest_block is None:
        latest_block = get_latest_block()
    stats = {
        "rescued": _rescue_stuck_confirmations(db),
        "stale_pending": _stale_pending_events(db),
        "suspicious_skips": _suspicious_skips(db),
    }
    stats.update(_crosscheck_payments(db, latest_block, fetch))
    db.commit()
    return stats
