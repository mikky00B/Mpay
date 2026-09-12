"""Processor tests — the core acceptance criterion: replay never double-credits."""
from __future__ import annotations

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import (
    ChainEvent,
    ChainEventStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    utcnow,
)
from app.processor import process_event, run_pending

HOT = "0xhot000000000000000000000000000000000009"


def _session(db_url):
    return get_sessionmaker(db_url)()


def _invoice(db, amount=10_000_123):
    m = Merchant(name="m", webhook_secret="s")
    db.add(m)
    db.flush()
    inv = Invoice(
        merchant_id=m.id,
        public_id="inv-1",
        amount_base_units=amount,
        receiving_address=HOT,
        status=InvoiceStatus.AWAITING_PAYMENT,
        expires_at=utcnow().replace(year=2100),
    )
    db.add(inv)
    db.commit()
    return inv


def _event(db, tx="0xtx1", log_index=0, amount=10_000_123, to=HOT):
    ev = ChainEvent(
        tx_hash=tx, log_index=log_index, block_number=100,
        contract_address="0xusdc", from_address="0xpayer",
        to_address=to, amount_base_units=amount,
        status=ChainEventStatus.PENDING,
    )
    db.add(ev)
    db.commit()
    return ev


def test_first_event_credits_and_moves_state(db_url):
    db = _session(db_url)
    inv = _invoice(db)
    ev = _event(db)

    outcome = process_event(db, ev)
    db.commit()

    assert outcome == "credited"
    assert inv.status is InvoiceStatus.CONFIRMING
    assert inv.paid_base_units == 10_000_123
    assert inv.detected_block == 100


def test_replayed_event_never_double_credits(db_url):
    """ACCEPTANCE CRITERION: same event twice -> credited then duplicate."""
    db = _session(db_url)
    inv = _invoice(db)
    ev = _event(db)

    assert process_event(db, ev) == "credited"
    db.commit()
    paid_after_first = inv.paid_base_units

    assert process_event(db, ev) == "duplicate"  # replay is a no-op
    db.commit()
    assert inv.paid_base_units == paid_after_first  # money unchanged
    assert ev.status is ChainEventStatus.PROCESSED


def test_unmatched_event_is_skipped(db_url):
    db = _session(db_url)
    _invoice(db)
    # wrong amount — no invoice matches
    ev = _event(db, tx="0xother", amount=999_999)
    assert process_event(db, ev) == "skipped"
    assert ev.status is ChainEventStatus.SKIPPED


def test_wrong_address_never_credits(db_url):
    db = _session(db_url)
    _invoice(db)
    ev = _event(db, tx="0xelsewhere", to="0xNOTTHEHOTADDRESS000000000000000000")
    assert process_event(db, ev) == "skipped"


def test_run_pending_processes_batch(db_url):
    db = _session(db_url)
    inv = _invoice(db)
    e1 = _event(db, tx="0xa", log_index=0)
    e2 = _event(db, tx="0xb", log_index=0, amount=999_999)  # unmatched

    outcomes = {o["event"]: o["outcome"] for o in run_pending(db)}
    db.commit()

    assert outcomes["0xa:0"] == "credited"
    assert outcomes["0xb:0"] == "skipped"
    assert inv.status is InvoiceStatus.CONFIRMING

