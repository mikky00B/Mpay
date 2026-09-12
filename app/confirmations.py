"""Confirmation counter + expiry sweep (build step 5).

On each sweep:
1. AWAITING_PAYMENT invoices past their TTL are EXPIRED (enqueue invoice.expired).
2. CONFIRMING invoices whose on-chain depth reached the threshold flip to
   CONFIRMED (enqueue invoice.confirmed; SETTLED follows on webhook success).

`latest_block` comes from settings.rpc_url via `eth_blockNumber`. In offline
mode (no rpc_url — local dev/tests) it returns None and depth checks are
skipped; tests inject block heights directly to drive the transition.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice, InvoiceStatus, utcnow
from app.state_machine import transition
from app.webhooks import enqueue

log = logging.getLogger(__name__)


def get_latest_block(rpc_url: str | None = None) -> int | None:
    """Current chain height via JSON-RPC; None on any failure / offline mode."""
    url = rpc_url if rpc_url is not None else get_settings().rpc_url
    if not url:
        return None
    try:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            timeout=10.0,
        )
        resp.raise_for_status()
        return int(resp.json()["result"], 16)
    except Exception as exc:
        log.warning("eth_blockNumber failed (%s): %s", url, exc)
        return None


def sweep(db: Session, latest_block: int | None = None) -> dict[str, int]:
    """Run one expiry + confirmation pass. Returns counters for logging/tests."""
    s = get_settings()
    if latest_block is None:
        latest_block = get_latest_block()
    stats = {"expired": 0, "confirmed": 0}
    now = utcnow()

    # 1) Expiry — only pre-payment states expire; real money never expires.
    awaiting = (
        db.execute(select(Invoice).where(Invoice.status == InvoiceStatus.AWAITING_PAYMENT))
        .scalars()
        .all()
    )
    for inv in awaiting:
        if inv.expires_at <= now:
            transition(inv, InvoiceStatus.EXPIRED)
            enqueue(db, inv, "invoice.expired")
            stats["expired"] += 1

    # 2) Confirmation depth.
    if latest_block is not None:
        confirming = (
            db.execute(select(Invoice).where(Invoice.status == InvoiceStatus.CONFIRMING))
            .scalars()
            .all()
        )
        for inv in confirming:
            depth = latest_block - (inv.detected_block or 0)
            if depth >= s.confirmation_threshold:
                # Block at which the Nth confirmation was reached.
                inv.confirmed_block = inv.detected_block + s.confirmation_threshold - 1
                transition(inv, InvoiceStatus.CONFIRMED)
                enqueue(db, inv, "invoice.confirmed")
                stats["confirmed"] += 1
                log.info("invoice %s CONFIRMED at depth %s", inv.public_id, depth)

    db.commit()
    return stats
