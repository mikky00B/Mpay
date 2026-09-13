"""Webhook signing + reliable delivery [D8] (build step 6).

Event types emitted: invoice.detected, invoice.confirmed, invoice.settled,
invoice.expired, invoice.reorgged [D19].

Semantics:
- One `webhook_deliveries` row per ATTEMPT: pending → delivered | failed.
- On failure with attempts remaining, a NEW pending row is created with
  attempt_no+1 and next_retry_at = now + exponential backoff. The failed row
  keeps the exact error/response code — a complete audit timeline.
- Signature: HMAC-SHA256 hex over the raw body bytes, keyed by the merchant's
  secret, sent as `X-Mpay-Signature`. The signed bytes are exactly the stored
  and sent bytes, so merchants can verify byte-for-byte.
- Delivery success of `invoice.confirmed` drives CONFIRMED → SETTLED (plan.md:
  SETTLED means "merchant notified successfully"), which enqueues the
  `invoice.settled` event.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DeliveryStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    WebhookDelivery,
    utcnow,
)
from app.state_machine import transition

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Mpay-Signature"


def host_is_private(url: str) -> bool:
    """SSRF guard [D22]: True if the URL's host is (or resolves to) a
    private / loopback / link-local address — i.e. a target a merchant could
    use to reach our own network. Unresolvable hosts are NOT flagged: the
    HTTP client's own ConnectError reports that failure path as before.
    Literal IPs skip DNS entirely."""
    try:
        host = (urlparse(url).hostname or "").strip("[]")
    except ValueError:
        return True  # unparseable URL — treat as unsafe
    if not host:
        return True
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        except Exception:
            return False
        ips = []
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    return any(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_unspecified or ip.is_reserved
        for ip in ips
    )


def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest of the raw body under the merchant secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_payload(invoice: Invoice, event_type: str) -> str:
    """Deterministic JSON body for a webhook (sorted keys, compact separators)."""
    payload = {
        "type": event_type,
        "created_at": utcnow().isoformat() + "Z",
        "data": {
            "invoice_id": invoice.public_id,
            "merchant_id": invoice.merchant_id,
            "status": invoice.status.value,
            "requested_base_units": invoice.requested_base_units,
            "amount_base_units": invoice.amount_base_units,
            "paid_base_units": invoice.paid_base_units,
            "token": invoice.token,
            "chain": invoice.chain,
            "receiving_address": invoice.receiving_address,
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def enqueue(db: Session, invoice: Invoice, event_type: str) -> WebhookDelivery | None:
    """Create a pending delivery for the invoice's merchant. Caller commits."""
    merchant = db.get(Merchant, invoice.merchant_id)
    if merchant is None:
        log.warning("enqueue: merchant %s not found", invoice.merchant_id)
        return None
    body = build_payload(invoice, event_type)
    delivery = WebhookDelivery(
        merchant_id=merchant.id,
        invoice_id=invoice.id,
        event_type=event_type,
        payload=body,
        signature=sign(merchant.webhook_secret, body.encode("utf-8")),
        attempt_no=1,
        status=DeliveryStatus.PENDING,
        next_retry_at=None,  # due immediately
    )
    db.add(delivery)
    db.flush()
    return delivery


def dispatch_due_deliveries(db: Session, http_client: httpx.Client | None = None) -> list[int]:
    """Deliver every due pending webhook. Returns ids successfully delivered.

    Injectable `http_client` for tests (httpx.MockTransport) — the retry,
    signing, and bookkeeping logic all run for real [D9].
    """
    s = get_settings()
    now = utcnow()
    due = (
        db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == DeliveryStatus.PENDING,
                (WebhookDelivery.next_retry_at.is_(None))
                | (WebhookDelivery.next_retry_at <= now),
            )
            .order_by(WebhookDelivery.id)
            .limit(50)
        )
        .scalars()
        .all()
    )

    own_client = http_client is None
    client = http_client or httpx.Client(timeout=s.webhook_timeout_seconds)
    delivered: list[int] = []

    try:
        for d in due:
            merchant = db.get(Merchant, d.merchant_id)
            if merchant is None or not merchant.webhook_url:
                d.status = DeliveryStatus.FAILED
                d.error = "merchant has no webhook URL configured"
                db.commit()
                continue

            # SSRF guard [D22]: a private target can NEVER become reachable by
            # retrying, so this is a terminal failure — no retry row is spawned.
            if not s.webhook_allow_private_hosts and host_is_private(merchant.webhook_url):
                d.status = DeliveryStatus.FAILED
                d.error = ("webhook URL blocked by SSRF guard: host is private/"
                           "loopback — the merchant must reconfigure it")
                db.commit()
                log.warning("webhook attempt %s blocked: private target %r",
                            d.id, merchant.webhook_url)
                continue

            ok, code, err = False, None, None
            try:
                resp = client.post(
                    merchant.webhook_url,
                    content=d.payload.encode("utf-8"),
                    headers={SIGNATURE_HEADER: d.signature, "Content-Type": "application/json"},
                )
                code = resp.status_code
                ok = 200 <= code < 300
                if not ok:
                    err = f"non-2xx response: {code}"
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"

            d.response_code = code
            if ok:
                d.status = DeliveryStatus.DELIVERED
                d.delivered_at = utcnow()
                d.error = None
                delivered.append(d.id)
                if d.event_type == "invoice.confirmed" and d.invoice_id is not None:
                    inv = db.get(Invoice, d.invoice_id)
                    if inv is not None and inv.status is InvoiceStatus.CONFIRMED:
                        transition(inv, InvoiceStatus.SETTLED)
                        enqueue(db, inv, "invoice.settled")
            else:
                d.status = DeliveryStatus.FAILED
                d.error = err
                if d.attempt_no < s.webhook_max_attempts:
                    delay = min(
                        s.webhook_backoff_base_seconds * (2 ** (d.attempt_no - 1)),
                        s.webhook_backoff_cap_seconds,
                    )
                    retry = WebhookDelivery(
                        merchant_id=d.merchant_id,
                        invoice_id=d.invoice_id,
                        event_type=d.event_type,
                        payload=d.payload,
                        signature=d.signature,
                        attempt_no=d.attempt_no + 1,
                        status=DeliveryStatus.PENDING,
                        next_retry_at=utcnow() + timedelta(seconds=delay),
                    )
                    db.add(retry)
                    log.info(
                        "webhook attempt %s failed (%s); retry #%s in %.1fs",
                        d.id, err, d.attempt_no + 1, delay,
                    )
            db.commit()
    finally:
        if own_client:
            client.close()
    return delivered

