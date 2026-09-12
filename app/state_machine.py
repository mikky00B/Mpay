"""Invoice state machine — the single authority for status transitions [D11].

Legal lifecycle (plan.md):

    CREATED -> AWAITING_PAYMENT
    AWAITING_PAYMENT -> DETECTED | EXPIRED
    DETECTED -> CONFIRMING
    CONFIRMING -> CONFIRMED
    CONFIRMED -> SETTLED

Reorg rollbacks [D19] — the reverse edges, used ONLY by the reconciliation
job when the chain itself invalidates a credited payment:

    CONFIRMED -> CONFIRMING -> DETECTED -> AWAITING_PAYMENT

SETTLED and EXPIRED remain terminal: a settled invoice was already confirmed
at threshold depth and the merchant was notified — unwinding it is an
operator decision, never an automatic one.

Anything else is a bug and raises `IllegalTransition` — loud failure beats
silent drift.
"""
from __future__ import annotations

from app.models import Invoice, InvoiceStatus, utcnow


class IllegalTransition(Exception):
    def __init__(self, current: InvoiceStatus, target: InvoiceStatus) -> None:
        super().__init__(f"illegal invoice transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


# The lifecycle, encoded as a table. This table IS the state machine.
LEGAL: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.CREATED: {InvoiceStatus.AWAITING_PAYMENT},
    InvoiceStatus.AWAITING_PAYMENT: {InvoiceStatus.DETECTED, InvoiceStatus.EXPIRED},
    InvoiceStatus.DETECTED: {InvoiceStatus.CONFIRMING, InvoiceStatus.AWAITING_PAYMENT},
    InvoiceStatus.CONFIRMING: {InvoiceStatus.CONFIRMED, InvoiceStatus.DETECTED},
    InvoiceStatus.CONFIRMED: {InvoiceStatus.SETTLED, InvoiceStatus.CONFIRMING},
    # Terminal — no exits.
    InvoiceStatus.SETTLED: set(),
    InvoiceStatus.EXPIRED: set(),
}


def transition(invoice: Invoice, target: InvoiceStatus) -> Invoice:
    """Move `invoice` to `target` if legal; raises IllegalTransition otherwise.

    Mutates the instance in place; the caller owns committing the session.
    """
    current = invoice.status
    if target == current:
        return invoice  # idempotent no-op — replays are safe
    if target not in LEGAL[current]:
        raise IllegalTransition(current, target)

    invoice.status = target
    # NOTE: confirmed_block is owned by the confirmation counter (sweep), which
    # knows the exact block of the Nth confirmation; transition stays pure.
    return invoice


def is_payable(invoice: Invoice) -> bool:
    """Whether new payments may still be credited to this invoice."""
    return invoice.status in {
        InvoiceStatus.AWAITING_PAYMENT,
        InvoiceStatus.DETECTED,
        InvoiceStatus.CONFIRMING,
        InvoiceStatus.CONFIRMED,
    }


def mark_expired_if_due(invoice: Invoice) -> bool:
    """Expire an AWAITING_PAYMENT invoice whose TTL has passed.

    Returns True if a transition happened. Payment states never expire —
    a late payment on a live invoice is still real money.
    """
    if invoice.status is InvoiceStatus.AWAITING_PAYMENT and invoice.expires_at <= utcnow():
        transition(invoice, InvoiceStatus.EXPIRED)
        return True
    return False
