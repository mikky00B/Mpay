"""HTTP API (build step 2) — merchants, invoices, internal ingest, delivery log.

Public surface:
- POST /merchants                       create merchant (webhook secret shown once)
- POST /merchants/{id}/invoices         create invoice (idempotent via key)
- GET  /invoices/{public_id}            invoice status + payments
- GET  /merchants/{id}/invoices         list invoices
- GET  /merchants/{id}/deliveries       webhook delivery log (plan.md requirement)
- POST /internal/events                 watcher ingest [D5], X-Internal-Key guarded
- GET  /health

Money crosses the API only as decimal strings [D4]; conversion is exact.
"""
from __future__ import annotations

import logging
import secrets as _secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import money
from app.config import get_settings
from app.db import get_db
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
from app.processor import run_pending
from app.state_machine import transition

log = logging.getLogger(__name__)
router = APIRouter()


def _iso_z(dt) -> str | None:
    """Serialize naive-UTC datetimes with an explicit Z suffix [D10]."""
    return dt.isoformat() + "Z" if dt is not None else None


# ---------------------------------------------------------------- serializers

def invoice_out(inv: Invoice) -> dict:
    return {
        "public_id": inv.public_id,
        "merchant_id": inv.merchant_id,
        "description": inv.description,
        "status": inv.status.value,
        "token": inv.token,
        "chain": inv.chain,
        "requested_amount": money.base_units_to_decimal_string(inv.requested_base_units),
        "payable_amount": money.base_units_to_decimal_string(inv.amount_base_units),
        "paid_amount": money.base_units_to_decimal_string(inv.paid_base_units),
        "receiving_address": inv.receiving_address,
        "detected_block": inv.detected_block,
        "confirmed_block": inv.confirmed_block,
        "expires_at": _iso_z(inv.expires_at),
        "created_at": _iso_z(inv.created_at),
    }


# ------------------------------------------------------------------- schemas

class MerchantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    webhook_url: str = ""


class InvoiceCreate(BaseModel):
    amount: str = Field(description="Decimal string, e.g. '25.50'")
    description: str = ""
    idempotency_key: str | None = Field(default=None, max_length=120)


class ChainEventIn(BaseModel):
    tx_hash: str = Field(min_length=10, max_length=80)
    log_index: int = Field(ge=0)
    block_number: int = Field(ge=0)
    block_hash: str | None = Field(default=None, max_length=80)
    contract_address: str = Field(min_length=10, max_length=64)
    from_address: str = Field(min_length=10, max_length=64)
    to_address: str = Field(min_length=10, max_length=64)
    amount_base_units: int = Field(gt=0)
    token: str = "USDC"
    raw_payload: str | None = None


class IngestBatch(BaseModel):
    events: list[ChainEventIn] = Field(min_length=1, max_length=500)


# -------------------------------------------------------------------- routes

@router.post("/merchants", status_code=201)
def create_merchant(body: MerchantCreate, db: Session = Depends(get_db)):
    """Create a merchant. The webhook secret is returned ONCE — store it."""
    secret = "whsec_" + _secrets.token_hex(24)
    m = Merchant(name=body.name, webhook_url=body.webhook_url, webhook_secret=secret)
    db.add(m)
    db.flush()
    return {
        "id": m.id,
        "name": m.name,
        "webhook_url": m.webhook_url,
        "webhook_secret": secret,  # shown once; rotate = new secret
    }


@router.post("/merchants/{merchant_id}/invoices", status_code=201)
def create_invoice(merchant_id: int, body: InvoiceCreate, db: Session = Depends(get_db)):
    """Create an invoice. Idempotent on `idempotency_key` per merchant."""
    s = get_settings()
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(404, "merchant not found")

    if body.idempotency_key:
        existing = db.execute(
            select(Invoice).where(
                Invoice.merchant_id == merchant_id,
                Invoice.idempotency_key == body.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"invoice": invoice_out(existing), "idempotent_replay": True}

    try:
        requested = money.parse_amount_to_base_units(body.amount)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    now = utcnow()
    inv = Invoice(
        merchant_id=merchant_id,
        public_id=_secrets.token_hex(16),
        description=body.description,
        requested_base_units=requested,
        amount_base_units=requested,  # finalized after flush assigns the id [D6]
        status=InvoiceStatus.CREATED,
        receiving_address=s.receiving_address,
        idempotency_key=body.idempotency_key,
        expires_at=now + timedelta(minutes=s.invoice_ttl_minutes),
    )
    db.add(inv)
    db.flush()  # assigns inv.id

    # [D6][D15] payable = requested + per-invoice unique offset, so an incoming
    # transfer (to_address, amount) maps to exactly one open invoice. The
    # partial unique index makes any residual collision fail LOUDLY here at
    # creation time instead of corrupting payment matching silently later.
    inv.amount_base_units = requested + money.unique_offset_for_invoice(inv.id)
    # CREATED -> AWAITING_PAYMENT immediately (v1 has no draft approval flow).
    # The transition must happen BEFORE the guarded flush: the partial index
    # only evaluates rows WHERE status = 'AWAITING_PAYMENT' [D15], so a
    # collision surfaces as this row flips status — catching it here, at
    # creation time, loudly (409) instead of corrupting matching later.
    transition(inv, InvoiceStatus.AWAITING_PAYMENT)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409,
            "an open invoice with this (address, amount) already exists — "
            "payable amounts must be unique among open invoices [D15]",
        ) from exc
    return {"invoice": invoice_out(inv), "idempotent_replay": False}


@router.get("/invoices/{public_id}")
def get_invoice(public_id: str, db: Session = Depends(get_db)):
    inv = db.execute(select(Invoice).where(Invoice.public_id == public_id)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "invoice not found")
    payments = [
        {
            "tx_hash": p.tx_hash,
            "log_index": p.log_index,
            "block_number": p.block_number,
            "amount": money.base_units_to_decimal_string(p.amount_base_units),
        }
        for p in inv.payments
    ]
    return {"invoice": invoice_out(inv), "payments": payments}


@router.get("/merchants/{merchant_id}/invoices")
def list_invoices(merchant_id: int, db: Session = Depends(get_db)):
    if db.get(Merchant, merchant_id) is None:
        raise HTTPException(404, "merchant not found")
    invs = db.execute(
        select(Invoice).where(Invoice.merchant_id == merchant_id).order_by(Invoice.id.desc())
    ).scalars().all()
    return {"invoices": [invoice_out(i) for i in invs]}


@router.get("/merchants/{merchant_id}/deliveries")
def list_deliveries(merchant_id: int, db: Session = Depends(get_db)):
    """Full webhook delivery log — the debugging surface required by plan.md."""
    if db.get(Merchant, merchant_id) is None:
        raise HTTPException(404, "merchant not found")
    rows = db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.merchant_id == merchant_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(200)
    ).scalars().all()
    return {
        "deliveries": [
            {
                "id": d.id,
                "invoice_id": d.invoice_id,
                "event_type": d.event_type,
                "attempt_no": d.attempt_no,
                "status": d.status.value,
                "response_code": d.response_code,
                "error": d.error,
                "next_retry_at": _iso_z(d.next_retry_at),
                "created_at": _iso_z(d.created_at),
                "delivered_at": _iso_z(d.delivered_at),
            }
            for d in rows
        ]
    }


def require_internal_key(x_internal_key: str | None = Header(default=None)) -> str:
    """Dependency: internal API key check. Runs BEFORE body validation, so
    unauthenticated requests are rejected without ever parsing their payload."""
    if x_internal_key != get_settings().internal_api_key:
        raise HTTPException(401, "invalid internal key")
    return x_internal_key


@router.post("/internal/events", status_code=202)
def ingest_events(
    batch: IngestBatch,
    db: Session = Depends(get_db),
    _key: str = Depends(require_internal_key),
):
    """Watcher ingest [D5]. Writes raw ChainEvents; processing is deferred.

    De-duplication: an indexed existence check first (cheap, no failed-flush
    state to unwind); the UNIQUE (tx_hash, log_index) index [D7] remains the
    concurrency backstop — a true race surfaces as an error and the watcher
    simply retries, since replays are always safe.
    """
    inserted, duplicates = 0, 0
    for ev in batch.events:
        existing = db.execute(
            select(ChainEvent.id).where(
                ChainEvent.tx_hash == ev.tx_hash,
                ChainEvent.log_index == ev.log_index,
            )
        ).scalar_one_or_none()
        if existing is not None:
            duplicates += 1
            continue
        db.add(
            ChainEvent(
                tx_hash=ev.tx_hash,
                log_index=ev.log_index,
                block_number=ev.block_number,
                block_hash=ev.block_hash,
                contract_address=ev.contract_address,
                from_address=ev.from_address,
                to_address=ev.to_address,
                amount_base_units=ev.amount_base_units,
                token=ev.token,
                raw_payload=ev.raw_payload,
                status=ChainEventStatus.PENDING,
            )
        )
        inserted += 1
    return {"accepted": inserted, "duplicates": duplicates}


