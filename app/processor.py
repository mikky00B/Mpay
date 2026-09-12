"""Event processor — applies raw ChainEvents to the invoice state machine.

This is build-order step 4 and the idempotency core [D7]:

- Ingest writes ChainEvents (pending). This module claims + applies them.
- Claim: status flipped pending -> processed inside the SAME transaction that
  inserts the Payment and moves the invoice. A replayed event finds a processed
  row and is a no-op — the DB unique index makes double-apply unrepresentable.
- Matching [D6]: a Transfer to the hot address for the exact payable amount of
  an AWAITING_PAYMENT invoice credits that invoice and moves it DETECTED.
- Later events for the same invoice (same or other tx) are recorded; if the
  invoice is already past DETECTED they are marked `skipped` in v1 (policy
  refinement for overpayment is a stretch goal).
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChainEvent,
    ChainEventStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    utcnow,
)
from app.state_machine import IllegalTransition, is_payable, transition

log = logging.getLogger(__name__)


def _find_matching_invoice(db: Session, event: ChainEvent) -> Invoice | None:
    """Match a Transfer event to a payable invoice by (to_address, amount).

    Case canonicalization [D17]: both sides are lowercase by construction —
    invoices store settings.receiving_address lowercased, events store
    to_address lowercased at ingest — so the plain `==` is correct and
    index-friendly. (SQLite compares case-sensitively; a checksummed hot
    address once made every real payment skip until this invariant existed.)

    Deterministic ordering [D15]: even if a (address, amount) collision ever
    sneaks in (e.g. an invoice created before the unique index existed), the
    lowest-id open invoice wins reproducibly instead of an arbitrary one.
    """
    stmt = (
        select(Invoice)
        .where(
            Invoice.receiving_address == event.to_address,
            Invoice.amount_base_units == event.amount_base_units,
            Invoice.status == InvoiceStatus.AWAITING_PAYMENT,
        )
        .order_by(Invoice.id)
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def process_event(db: Session, event: ChainEvent) -> str:
    """Apply one chain event. Returns one of: 'credited', 'skipped', 'duplicate'.

    Idempotent: calling twice with the same event returns 'duplicate' the second
    time and leaves state untouched.
    """
    if event.status is ChainEventStatus.PROCESSED:
        return "duplicate"

    # Claim semantics without a DB round-trip race: because a single process
    # (the API) applies events and ingest dedupes on the unique index, checking
    # + flipping inside one transaction/commit boundary is safe here. A
    # SELECT ... FOR UPDATE claim will be added with the Postgres deployment.
    if event.status is not ChainEventStatus.PENDING:
        return "duplicate"

    invoice = _find_matching_invoice(db, event)
    if invoice is None:
        event.status = ChainEventStatus.SKIPPED
        event.processed_at = utcnow()
        return "skipped"

    payment = Payment(
        invoice_id=invoice.id,
        chain_event_id=event.id,
        tx_hash=event.tx_hash,
        log_index=event.log_index,
        block_number=event.block_number,
        amount_base_units=event.amount_base_units,
    )
    db.add(payment)

    invoice.paid_base_units += event.amount_base_units
    invoice.detected_block = event.block_number
    transition(invoice, InvoiceStatus.DETECTED)
    transition(invoice, InvoiceStatus.CONFIRMING)

    event.status = ChainEventStatus.PROCESSED
    event.processed_at = utcnow()
    log.info("credited event %s:%s to invoice %s", event.tx_hash, event.log_index, invoice.public_id)
    return "credited"


def run_pending(db: Session, limit: int = 100) -> list[dict[str, str]]:
    """Process all pending events; returns per-event outcomes for logging/tests."""
    stmt = select(ChainEvent).where(ChainEvent.status == ChainEventStatus.PENDING).limit(limit)
    outcomes: list[dict[str, str]] = []
    for event in db.execute(stmt).scalars():
        try:
            outcome = process_event(db, event)
            db.commit()
        except IllegalTransition:
            # e.g. an expired invoice got paid — never double-apply; mark and move on.
            db.rollback()
            event.status = ChainEventStatus.SKIPPED
            event.processed_at = utcnow()
            db.commit()
            outcome = "skipped"
        outcomes.append({"event": f"{event.tx_hash}:{event.log_index}", "outcome": outcome})
    return outcomes
