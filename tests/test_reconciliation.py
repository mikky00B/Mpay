"""Reconciliation tests [D18] — DB-vs-chain consistency, behavior against the
real temp-file DB per [D9]. Chain access is injected; no network anywhere."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import (
    ChainEvent,
    ChainEventStatus,
    DeliveryStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    Payment,
    WebhookDelivery,
    utcnow,
)
from app.reconciliation import RpcUnavailable, reconcile

HOT = "0xhot000000000000000000000000000000000009"


def _session(db_url):
    return get_sessionmaker(db_url)()  # noqa: F821  (conftest injects)


def _merchant(db):
    m = Merchant(name="m", webhook_url="http://hooks.test/acme", webhook_secret="s")
    db.add(m)
    db.flush()
    return m


def _invoice(db, m, status=InvoiceStatus.AWAITING_PAYMENT, amount=1_000_000,
             public_id="inv-x", detected_block=None):
    inv = Invoice(
        merchant_id=m.id, public_id=public_id, amount_base_units=amount,
        receiving_address=HOT, status=status, detected_block=detected_block,
        expires_at=utcnow().replace(year=2100),
    )
    db.add(inv)
    db.commit()
    return inv


def _event(db, tx="0xtx1", to=HOT, amount=1_000_000,
           status=ChainEventStatus.PENDING, block_number=100,
           block_hash="0xhash1", age_seconds=0):
    ev = ChainEvent(
        tx_hash=tx, log_index=0, block_number=block_number, block_hash=block_hash,
        contract_address="0xusdc", from_address="0xpayer", to_address=to,
        amount_base_units=amount, status=status, created_at=utcnow() - timedelta(seconds=age_seconds),
    )
    db.add(ev)
    db.commit()
    return ev


def _payment(db, inv, ev):
    p = Payment(
        invoice_id=inv.id, chain_event_id=ev.id, tx_hash=ev.tx_hash,
        log_index=ev.log_index, block_number=ev.block_number,
        amount_base_units=ev.amount_base_units,
    )
    db.add(p)
    db.commit()
    return p


def _delivery(db, inv, event_type="invoice.confirmed",
              status=DeliveryStatus.FAILED):
    d = WebhookDelivery(
        merchant_id=inv.merchant_id, invoice_id=inv.id, event_type=event_type,
        payload="{}", signature="sig", attempt_no=1, status=status,
    )
    db.add(d)
    db.commit()
    return d


# ---------------------------------------------------------------- check 1

def test_rescues_confirmed_invoice_with_all_failed_deliveries(db_url):
    db = _session(db_url)
    m = _merchant(db)
    inv = _invoice(db, m, status=InvoiceStatus.CONFIRMED)
    _delivery(db, inv, status=DeliveryStatus.FAILED)
    _delivery(db, inv, status=DeliveryStatus.FAILED)

    stats = reconcile(db)
    assert stats["rescued"] == 1

    pending = db.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.invoice_id == inv.id,
            WebhookDelivery.status == DeliveryStatus.PENDING,
        )
    ).scalars().all()
    assert len(pending) == 1

    # Idempotent per pass: the fresh PENDING row stops further rescues.
    stats = reconcile(db)
    assert stats["rescued"] == 0


def test_rescue_skips_invoices_with_pending_or_delivered(db_url):
    db = _session(db_url)
    m = _merchant(db)
    inv_pending = _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-p")
    _delivery(db, inv_pending, status=DeliveryStatus.PENDING)
    inv_done = _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-d")
    _delivery(db, inv_done, status=DeliveryStatus.DELIVERED)
    inv_none = _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-n")

    stats = reconcile(db)
    assert stats["rescued"] == 1  # only inv-none (never notified at all)


def test_rescue_respects_per_sweep_cap(db_url, monkeypatch):
    monkeypatch.setenv("RECONCILIATION_RESCUE_CAP", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    db = _session(db_url)
    m = _merchant(db)
    for i in range(3):
        inv = _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id=f"inv-{i}")
        _delivery(db, inv, status=DeliveryStatus.FAILED)

    stats = reconcile(db)
    assert stats["rescued"] == 1
    get_settings.cache_clear()


# ---------------------------------------------------------------- check 2

def test_stale_pending_events_are_flagged(db_url):
    db = _session(db_url)
    m = _merchant(db)
    _invoice(db, m)
    _event(db, tx="0xold", age_seconds=400)          # past the 300s threshold
    _event(db, tx="0xfresh", age_seconds=10)          # recent — not stale

    stats = reconcile(db)
    assert stats["stale_pending"] == 1


# ---------------------------------------------------------------- check 3

def test_suspicious_skips_matching_open_invoice(db_url):
    db = _session(db_url)
    m = _merchant(db)
    _invoice(db, m, amount=1_000_000)                 # open, matches exactly
    _event(db, tx="0xskip", amount=1_000_000, status=ChainEventStatus.SKIPPED)
    _event(db, tx="0xok", amount=999, status=ChainEventStatus.SKIPPED)  # no match

    stats = reconcile(db)
    assert stats["suspicious_skips"] == 1


# ---------------------------------------------------------------- check 4

def _confirmed_payment(db):
    m = _merchant(db)
    inv = _invoice(db, m, status=InvoiceStatus.CONFIRMED, detected_block=100)
    ev = _event(db, block_number=100, block_hash="0xhash1")
    ev.status = ChainEventStatus.PROCESSED
    db.commit()
    p = _payment(db, inv, ev)
    return inv, ev, p


def test_crosscheck_passes_when_receipt_matches(db_url):
    db = _session(db_url)
    inv, ev, p = _confirmed_payment(db)

    def fetch(tx):
        assert tx == ev.tx_hash
        return {"blockHash": "0xhash1", "status": "0x1"}

    stats = reconcile(db, latest_block=112, fetch=fetch)
    assert stats["checked"] == 1 and stats["mismatched"] == 0
    db.refresh(inv)
    assert inv.status is InvoiceStatus.CONFIRMED


def test_crosscheck_orphans_missing_receipt_within_safety_window(db_url):
    """[D19] payment gone from the chain within the reorg window -> orphan."""
    db = _session(db_url)
    inv, ev, p = _confirmed_payment(db)

    stats = reconcile(db, latest_block=130, fetch=lambda tx: None)  # depth 30 < 60
    assert stats["mismatched"] == 1 and stats["orphaned"] == 1

    db.refresh(p)
    assert p.orphaned_at is not None
    db.refresh(ev)
    assert ev.status is ChainEventStatus.ORPHANED
    db.refresh(inv)
    assert inv.status is InvoiceStatus.AWAITING_PAYMENT
    assert inv.paid_base_units == 0
    assert inv.detected_block is None and inv.confirmed_block is None
    types = [
        d.event_type
        for d in db.execute(select(WebhookDelivery)
                            .where(WebhookDelivery.invoice_id == inv.id))
        .scalars().all()
    ]
    assert "invoice.reorgged" in types


def test_reorged_invoice_can_be_repaid(db_url):
    """After a reorg the same payable amount re-arms and a new tx credits."""
    db = _session(db_url)
    inv, ev, p = _confirmed_payment(db)
    reconcile(db, latest_block=130, fetch=lambda tx: None)
    db.refresh(inv)
    assert inv.status is InvoiceStatus.AWAITING_PAYMENT

    # A new on-chain payment for the same unique amount arrives:
    from app.processor import process_event

    ev2 = _event(db, tx="0xnewtx", amount=inv.amount_base_units)
    assert process_event(db, ev2) == "credited"
    db.commit()
    db.refresh(inv)
    assert inv.status is InvoiceStatus.CONFIRMING
    assert inv.paid_base_units == inv.amount_base_units


def test_settled_invoice_never_auto_rolled_back(db_url):
    """[D19] policy: SETTLED is terminal — the merchant was already told."""
    db = _session(db_url)
    m = _merchant(db)
    inv = _invoice(db, m, status=InvoiceStatus.SETTLED, detected_block=100)
    ev = _event(db, block_number=100, block_hash="0xhash1")
    ev.status = ChainEventStatus.PROCESSED
    db.commit()
    p = _payment(db, inv, ev)

    stats = reconcile(db, latest_block=130, fetch=lambda tx: None)
    assert stats["orphaned"] == 1
    db.refresh(inv)
    assert inv.status is InvoiceStatus.SETTLED  # untouched
    db.refresh(p)
    assert p.orphaned_at is not None            # audit rows still marked
    db.refresh(ev)
    assert ev.status is ChainEventStatus.ORPHANED


def test_crosscheck_flags_block_hash_mismatch(db_url):
    db = _session(db_url)
    inv, ev, p = _confirmed_payment(db)

    stats = reconcile(db, latest_block=130,
                      fetch=lambda tx: {"blockHash": "0xdifferent"})
    assert stats["mismatched"] == 1


def test_crosscheck_rpc_failure_voids_the_pass(db_url):
    db = _session(db_url)
    m = _merchant(db)
    _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-a", detected_block=100)
    ev1 = _event(db, tx="0x1", block_number=100)
    _payment(db, _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-b",
                          detected_block=100), ev1)
    ev2 = _event(db, tx="0x2", block_number=101)
    _payment(db, _invoice(db, m, status=InvoiceStatus.CONFIRMED, public_id="inv-c",
                          detected_block=101), ev2)
    calls = {"n": 0}

    def flaky(tx):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"blockHash": "0xhash1"}
        raise RpcUnavailable("connection reset")

    stats = reconcile(db, latest_block=130, fetch=flaky)
    # First payment verified, then the RPC died — pass aborted, not counted on.
    assert stats["checked"] == 1


def test_offline_mode_skips_crosscheck(db_url):
    db = _session(db_url)
    _confirmed_payment(db)

    def must_not_be_called(tx):
        raise AssertionError("cross-check must not run offline")

    stats = reconcile(db, latest_block=None, fetch=must_not_be_called)
    assert stats["checked"] == 0
