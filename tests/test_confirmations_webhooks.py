"""Confirmations, expiry, and webhook delivery — behavioral acceptance tests [D9].

Webhook HTTP is intercepted with httpx.MockTransport; retry, signing, and
delivery bookkeeping run for real against the real DB.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import (
    DeliveryStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    WebhookDelivery,
    utcnow,
)
from app.state_machine import transition
from app.webhooks import SIGNATURE_HEADER, dispatch_due_deliveries, enqueue

HOT = "0xhot000000000000000000000000000000000009"


def _session(db_url):
    return get_sessionmaker(db_url)()


def _confirmed_invoice(db, url="http://hooks.test/acme", secret="whsec_test"):
    m = Merchant(name="m", webhook_url=url, webhook_secret=secret)
    db.add(m)
    db.flush()
    inv = Invoice(
        merchant_id=m.id, public_id="inv-1", amount_base_units=10_000_000,
        receiving_address=HOT, status=InvoiceStatus.AWAITING_PAYMENT,
        expires_at=utcnow().replace(year=2100),
    )
    db.add(inv)
    db.commit()
    transition(inv, InvoiceStatus.DETECTED)
    transition(inv, InvoiceStatus.CONFIRMING)
    inv.detected_block = 100
    transition(inv, InvoiceStatus.CONFIRMED)
    enqueue(db, inv, "invoice.confirmed")
    db.commit()
    return m, inv


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hooks.test")


def test_confirmation_depth_flips_to_confirmed(db_url):
    db = _session(db_url)
    m = Merchant(name="m", webhook_secret="s")
    db.add(m)
    db.flush()
    inv = Invoice(
        merchant_id=m.id, public_id="inv-d", amount_base_units=1_000_000,
        receiving_address=HOT, status=InvoiceStatus.CONFIRMING,
        detected_block=500, expires_at=utcnow().replace(year=2100),
    )
    db.add(inv)
    db.commit()

    from app.confirmations import sweep

    stats = sweep(db, latest_block=508)  # depth 8 < threshold 12
    assert stats["confirmed"] == 0
    db.refresh(inv)
    assert inv.status is InvoiceStatus.CONFIRMING

    stats = sweep(db, latest_block=512)  # depth 12 -> CONFIRMED
    assert stats["confirmed"] == 1
    db.refresh(inv)
    assert inv.status is InvoiceStatus.CONFIRMED
    assert inv.confirmed_block == 511  # block of the 12th confirmation
    deliveries = db.execute(select(WebhookDelivery)).scalars().all()
    assert [d.event_type for d in deliveries] == ["invoice.confirmed"]


def test_expired_after_ttl(db_url):
    db = _session(db_url)
    m = Merchant(name="m", webhook_secret="s")
    db.add(m)
    db.flush()
    inv = Invoice(
        merchant_id=m.id, public_id="inv-e", amount_base_units=1_000_000,
        receiving_address=HOT, status=InvoiceStatus.AWAITING_PAYMENT,
        expires_at=utcnow().replace(year=2000),
    )
    db.add(inv)
    db.commit()

    from app.confirmations import sweep

    stats = sweep(db, latest_block=None)
    assert stats["expired"] == 1
    db.refresh(inv)
    assert inv.status is InvoiceStatus.EXPIRED

    # ...and a paid invoice never expires:
    inv2 = Invoice(
        merchant_id=m.id, public_id="inv-e2", amount_base_units=2_000_000,
        receiving_address=HOT, status=InvoiceStatus.CONFIRMING,
        detected_block=1, expires_at=utcnow().replace(year=2000),
    )
    db.add(inv2)
    db.commit()
    sweep(db, latest_block=None)
    db.refresh(inv2)
    assert inv2.status is InvoiceStatus.CONFIRMING  # untouched


def test_down_webhook_retried_then_delivered(db_url):
    """ACCEPTANCE CRITERION: down endpoint -> retries -> eventually delivered."""
    db = _session(db_url)
    m, inv = _confirmed_invoice(db)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 1:  # "down" for the first attempt only
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"ok": True})

    dispatch_due_deliveries(db, http_client=_client(handler))
    rows = db.execute(select(WebhookDelivery).order_by(WebhookDelivery.id)).scalars().all()
    assert len(rows) == 2
    assert rows[0].status is DeliveryStatus.FAILED
    assert rows[0].error and "ConnectError" in rows[0].error
    assert rows[1].attempt_no == 2 and rows[1].status is DeliveryStatus.PENDING

    # Backoff not yet elapsed -> dispatcher must NOT call again
    dispatch_due_deliveries(db, http_client=_client(handler))
    assert calls["n"] == 1

    # Force the retry due -> delivered; invoice SETTLED + settled event queued
    retry = db.execute(
        select(WebhookDelivery).where(WebhookDelivery.attempt_no == 2)
    ).scalar_one()
    retry.next_retry_at = utcnow()
    db.commit()
    dispatch_due_deliveries(db, http_client=_client(handler))

    db.refresh(inv)
    assert inv.status is InvoiceStatus.SETTLED
    events = [
        d.event_type
        for d in db.execute(select(WebhookDelivery).order_by(WebhookDelivery.id)).scalars().all()
    ]
    assert events == ["invoice.confirmed", "invoice.confirmed", "invoice.settled"]
    assert calls["n"] == 2  # second HTTP call happened on the due retry


def test_exhausted_retries_end_failed(db_url):
    db = _session(db_url)
    m, inv = _confirmed_invoice(db)

    def always_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    for _ in range(get_settings().webhook_max_attempts):
        for d in db.execute(
            select(WebhookDelivery).where(WebhookDelivery.status == DeliveryStatus.PENDING)
        ).scalars().all():
            d.next_retry_at = utcnow()
        db.commit()
        dispatch_due_deliveries(db, http_client=_client(always_down))

    rows = db.execute(select(WebhookDelivery).order_by(WebhookDelivery.id)).scalars().all()
    assert len(rows) == get_settings().webhook_max_attempts
    assert all(r.status is DeliveryStatus.FAILED for r in rows)
    assert rows[-1].attempt_no == get_settings().webhook_max_attempts
    db.refresh(inv)
    assert inv.status is InvoiceStatus.CONFIRMED  # never settled — honest state


def test_payload_signature_is_verifiable_hmac(db_url):
    db = _session(db_url)
    secret = "whsec_test"
    m, inv = _confirmed_invoice(db, secret=secret)

    def capture(request: httpx.Request) -> httpx.Response:
        body = request.read()
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert request.headers[SIGNATURE_HEADER] == expected  # merchant-verifiable
        parsed = json.loads(body)
        assert parsed["type"] == "invoice.confirmed"
        assert parsed["data"]["invoice_id"] == "inv-1"
        return httpx.Response(200)

    dispatch_due_deliveries(db, http_client=_client(capture))
    db.refresh(inv)
    assert inv.status is InvoiceStatus.SETTLED


