"""SQLAlchemy models — the five core entities from plan.md [D1 in plan].

Design notes:
- Money is always integer base units (bigint) [D4]; USDC has 6 decimals.
- `chain_events` has a UNIQUE index on (tx_hash, log_index) [D7]: the DB itself
  makes duplicate on-chain events unrepresentable, which is what makes
  reprocessing/idempotency safe.
- `payments` also carries a UNIQUE (tx_hash, log_index) so the same log can
  never be credited twice even if events were somehow re-derived.
- No dialect-specific column types (per [D3]) — works on SQLite and Postgres.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC now — DB convention is naive-UTC everywhere (see app/db.py)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InvoiceStatus(str, enum.Enum):
    """Invoice lifecycle per plan.md. Order matters: it is a state machine."""

    CREATED = "CREATED"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    DETECTED = "DETECTED"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    SETTLED = "SETTLED"
    EXPIRED = "EXPIRED"


class ChainEventStatus(str, enum.Enum):
    PENDING = "pending"      # raw, not yet applied to the state machine
    PROCESSED = "processed"  # applied successfully
    SKIPPED = "skipped"      # applied, but irrelevant (e.g. no matching invoice)
    ORPHANED = "orphaned"    # was processed, then reorged off the chain [D19]


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"        # exhausted max attempts


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # Webhook target + HMAC secret (shown once at creation, stored hashed in prod)
    webhook_url: Mapped[str] = mapped_column(String(500), default="")
    webhook_secret: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoices: Mapped[list[Invoice]] = relationship(back_populates="merchant")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        # The matching key for payments [D6][D15]: shared address + unique
        # amount. Partial UNIQUE: only OPEN invoices are guarded — settled or
        # expired invoices must be able to repeat past amounts. Supported by
        # both SQLite and Postgres (no dialect conflict with [D3]).
        Index(
            "uq_invoices_open_address_amount",
            "receiving_address",
            "amount_base_units",
            unique=True,
            sqlite_where=text("status = 'AWAITING_PAYMENT'"),
            postgresql_where=text("status = 'AWAITING_PAYMENT'"),
        ),
        CheckConstraint("amount_base_units > 0", name="ck_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    # Public reference returned to the payer/merchant systems.
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    token: Mapped[str] = mapped_column(String(20), default="USDC")
    chain: Mapped[str] = mapped_column(String(20), default="ethereum")

    # Money [D4]: integer base units. 1 USDC == 1_000_000.
    # requested = what the merchant asked for; amount = what the payer must
    # actually send (requested + unique per-invoice offset) [D6].
    requested_base_units: Mapped[int] = mapped_column(BigInteger, default=0)
    amount_base_units: Mapped[int] = mapped_column(BigInteger)
    paid_base_units: Mapped[int] = mapped_column(BigInteger, default=0)

    receiving_address: Mapped[str] = mapped_column(String(64))

    status: Mapped[InvoiceStatus] = mapped_column(
        SAEnum(InvoiceStatus, native_enum=False, length=20, validate_strings=True),
        default=InvoiceStatus.AWAITING_PAYMENT,
        index=True,
    )

    # Idempotency key supplied by the merchant's create call (optional).
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    # Block in which the payment was first seen (drives confirmation depth).
    detected_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    merchant: Mapped[Merchant] = relationship(back_populates="invoices")
    payments: Mapped[list[Payment]] = relationship(back_populates="invoice")


class ChainEvent(Base):
    """Raw, unprocessed record of an on-chain event (plan.md entity #3).

    Written first by the watcher (via the ingest API), processed idempotently
    later by the event processor. The UNIQUE (tx_hash, log_index) index is the
    idempotency cornerstone [D7]: a replayed event collides at the DB level.
    """

    __tablename__ = "chain_events"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_chain_events_tx_log"),
        Index("ix_chain_events_match", "to_address", "amount_base_units"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tx_hash: Mapped[str] = mapped_column(String(80))
    log_index: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)

    contract_address: Mapped[str] = mapped_column(String(64))
    from_address: Mapped[str] = mapped_column(String(64))
    to_address: Mapped[str] = mapped_column(String(64))
    amount_base_units: Mapped[int] = mapped_column(BigInteger)
    token: Mapped[str] = mapped_column(String(20), default="USDC")

    status: Mapped[ChainEventStatus] = mapped_column(
        SAEnum(ChainEventStatus, native_enum=False, length=20, validate_strings=True),
        default=ChainEventStatus.PENDING,
        index=True,
    )
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    """A processed payment matched to an invoice (plan.md entity #4)."""

    __tablename__ = "payments"
    __table_args__ = (
        # Belt-and-braces idempotency: the same log entry can never be credited
        # twice, even if ChainEvents were somehow re-derived from scratch.
        UniqueConstraint("tx_hash", "log_index", name="uq_payments_tx_log"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    chain_event_id: Mapped[int] = mapped_column(ForeignKey("chain_events.id"))

    tx_hash: Mapped[str] = mapped_column(String(80))
    log_index: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger)
    amount_base_units: Mapped[int] = mapped_column(BigInteger)
    # Set when the chain reorged this payment away [D19]. Rows are NEVER
    # deleted — the audit trail must show credited-then-orphaned history.
    orphaned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class WebhookDelivery(Base):
    """One attempt to notify a merchant (plan.md entity #5).

    One row per HTTP attempt: pending → delivered | failed. A failed attempt
    that still has retries left is followed by a NEW pending row with the next
    `next_retry_at`, giving a complete, auditable delivery timeline [D8].
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40))  # e.g. "invoice.confirmed"
    payload: Mapped[str] = mapped_column(Text)           # exact JSON body sent
    signature: Mapped[str] = mapped_column(String(128))  # HMAC hex, precomputed

    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, native_enum=False, length=20, validate_strings=True),
        default=DeliveryStatus.PENDING,
        index=True,
    )
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

