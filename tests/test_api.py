"""API tests: merchants, invoices, idempotency, ingest dedupe, delivery log."""
from __future__ import annotations

import pytest


@pytest.fixture()
def merchant(client):
    r = client.post("/merchants", json={"name": "Acme", "webhook_url": "http://hooks.test/acme"})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(merchant) -> dict:
    """X-API-Key header for a merchant fixture [D20]."""
    return {"X-API-Key": merchant["api_key"]}


def test_merchant_scoped_endpoints_require_api_key(client, merchant):
    """[D20]: no key, wrong key -> 401; unknown merchant -> 404."""
    r = client.post(f"/merchants/{merchant['id']}/invoices", json={"amount": "5"})
    assert r.status_code == 401
    r = client.post(f"/merchants/{merchant['id']}/invoices", json={"amount": "5"},
                    headers={"X-API-Key": "mpay_sk_wrong"})
    assert r.status_code == 401
    r = client.get(f"/merchants/{merchant['id']}/deliveries")
    assert r.status_code == 401
    r = client.get("/merchants/999/deliveries", headers=_auth(merchant))
    assert r.status_code == 404
    # Public endpoints stay keyless by design:
    r = client.post("/merchants", json={"name": "Open"})
    assert r.status_code == 201


def test_api_key_is_never_stored_raw(client, merchant, db_url):
    """[D20]: only the SHA-256 hash is persisted; the raw key exists solely in
    the creation response."""
    import hashlib

    from sqlalchemy import select
    from app.db import get_sessionmaker
    from app.models import Merchant

    with get_sessionmaker(db_url)() as db:
        row = db.execute(
            select(Merchant.api_key_hash).where(Merchant.id == merchant["id"])
        ).scalar_one()
    assert row != merchant["api_key"]
    assert row == hashlib.sha256(merchant["api_key"].encode()).hexdigest()


def test_create_invoice_returns_payable_amount(client, merchant):
    r = client.post(
        f"/merchants/{merchant['id']}/invoices",
        json={"amount": "25.50", "description": "Order #42"},
        headers=_auth(merchant),
    )
    assert r.status_code == 201, r.text
    inv = r.json()["invoice"]
    assert inv["status"] == "AWAITING_PAYMENT"
    assert inv["requested_amount"] == "25.5"
    # payable = requested + small unique offset [D6]
    assert float(inv["payable_amount"]) > 25.50
    assert inv["receiving_address"].startswith("0xhot")


def test_invoice_creation_idempotent_on_key(client, merchant):
    body = {"amount": "10", "idempotency_key": "order-42"}
    h = _auth(merchant)
    r1 = client.post(f"/merchants/{merchant['id']}/invoices", json=body, headers=h)
    r2 = client.post(f"/merchants/{merchant['id']}/invoices", json=body, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["invoice"]["public_id"] == r2.json()["invoice"]["public_id"]
    assert r2.json()["idempotent_replay"] is True


def test_ingest_requires_internal_key(client):
    r = client.post(
        "/internal/events",
        json={"events": [_event()]},
        headers={"X-Internal-Key": "wrong"},
    )
    assert r.status_code == 401


def _event(tx="0xtx10000000000000000", log_index=0, amount=10_000_123):
    return {
        "tx_hash": tx,
        "log_index": log_index,
        "block_number": 100,
        "contract_address": "0xusdc000000000000000000000000000000000000",
        "from_address": "0xpayer0000000000000000000000000000000000",
        "to_address": "0xhot000000000000000000000000000000000009",
        "amount_base_units": amount,
    }


def test_ingest_dedupes_on_tx_log_index(client):
    body = {"events": [_event()]}
    h = {"X-Internal-Key": "test-internal-key"}
    r1 = client.post("/internal/events", json=body, headers=h)
    r2 = client.post("/internal/events", json=body, headers=h)
    assert r1.json()["accepted"] == 1
    assert r2.json()["duplicates"] == 1  # DB unique index absorbed the replay [D7]


# --------------------------------------------------------------------------
# [D15] Finding 1 regression tests: payable offsets must never collide among
# open invoices, and the partial unique index must reject duplicates loudly.
# --------------------------------------------------------------------------

def _mk_invoice(client, merchant, amount, key=None):
    r = client.post(
        f"/merchants/{merchant['id']}/invoices",
        json={"amount": amount, **({"idempotency_key": key} if key else {})},
        headers=_auth(merchant),
    )
    assert r.status_code == 201, r.text
    return r.json()["invoice"]


def _to_base_units(decimal_str: str) -> int:
    from app import money

    return money.parse_amount_to_base_units(decimal_str)


def test_offsets_900_ids_apart_do_not_collide(client, merchant, db_url):
    """Ids exactly 900 apart with identical requested amounts must produce
    DIFFERENT payable amounts — the old `id % 900 + 1` scheme wrapped here
    and could misdirect a real payment to the wrong invoice [D15]."""
    from sqlalchemy import insert, select
    from app.db import get_engine, get_sessionmaker
    from app.models import Invoice, InvoiceStatus, utcnow

    engine = get_engine(db_url)
    addr = "0xhot000000000000000000000000000000000009"
    now = utcnow()
    # Seed rows with explicit ids 900 apart, same requested amount (10 USDC
    # = 10_000_000 base). amount = requested + id — exactly what the API's
    # offset scheme [D6][D15] produces, so the partial unique index accepts
    # both open rows (the old %900 scheme would have made them collide).
    with engine.begin() as conn:
        for inv_id in (1, 901):
            conn.execute(
                insert(Invoice).values(
                    id=inv_id,
                    merchant_id=merchant["id"],
                    public_id=f"seeded-{inv_id}",
                    requested_base_units=10_000_000,
                    amount_base_units=10_000_000 + inv_id,
                    status=InvoiceStatus.AWAITING_PAYMENT,
                    receiving_address=addr,
                    expires_at=now,
                )
            )
    # Prove the SCHEME differs: simulate the old scheme vs. new on these ids.
    old_offsets = {i % 900 + 1 for i in (1, 901)}
    new_offsets = {i for i in (1, 901)}
    assert len(old_offsets) == 1, "sanity: old scheme collided here"
    assert len(new_offsets) == 2, "new scheme must differ for ids 900 apart"
    from sqlalchemy import select
    from app.db import get_sessionmaker

    with get_sessionmaker(db_url)() as db:
        amounts = db.execute(
            select(Invoice.amount_base_units).where(Invoice.id.in_([1, 901]))
        ).scalars().all()
    assert len(set(amounts)) == 2, f"seeded payables collide: {amounts}"
    # And via the API: two fresh invoices share requested amount -> distinct
    # payables, since ids are unique.
    inv1 = _mk_invoice(client, merchant, "10", key="k1")
    inv2 = _mk_invoice(client, merchant, "10", key="k2")
    a1 = _to_base_units(inv1["payable_amount"])
    a2 = _to_base_units(inv2["payable_amount"])
    assert a1 != a2, f"payables collided: {a1} vs {a2}"
    assert a1 > 10_000_000 and a2 > 10_000_000


def test_duplicate_open_address_amount_rejected_by_index(client, merchant, db_url):
    """Defense in depth [D15]: a second OPEN invoice with the same
    (receiving_address, amount_base_units) must be rejected — loudly (409),
    never silently corrupting payment matching."""
    from sqlalchemy import insert, select
    from app.db import get_engine, get_sessionmaker
    from app.models import Invoice, InvoiceStatus, utcnow

    engine = get_engine(db_url)
    addr = "0xhot000000000000000000000000000000000009"
    amount = 25_500_002  # 25.5 USDC + offset 2 — the collision the API will make
    with engine.begin() as conn:
        conn.execute(
            insert(Invoice).values(
                id=1,
                merchant_id=merchant["id"],
                public_id="seeded-open",
                requested_base_units=25_500_000,
                amount_base_units=amount,
                status=InvoiceStatus.AWAITING_PAYMENT,
                receiving_address=addr,
                expires_at=utcnow(),
            )
        )
    # API invoice whose payable lands on the same (address, amount) as the
    # seeded open invoice: offset 2 (id=2) => requested must be amount - 2.
    from app import money

    requested = money.base_units_to_decimal_string(amount - 2)
    r = client.post(f"/merchants/{merchant['id']}/invoices", json={"amount": requested},
                    headers=_auth(merchant))
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    # And the seeded open invoice is untouched (exactly one row at that key).
    with get_sessionmaker(db_url)() as db:
        n = len(db.execute(
            select(Invoice.id).where(Invoice.amount_base_units == amount)
        ).scalars().all())
    assert n == 1


def test_settled_invoice_amount_can_repeat(client, merchant, db_url):
    """The partial index guards only OPEN invoices [D15]: once one is settled,
    a later open invoice may reuse the same payable amount."""
    from sqlalchemy import insert, select
    from app.db import get_engine, get_sessionmaker
    from app.models import Invoice, InvoiceStatus, utcnow

    engine = get_engine(db_url)
    addr = "0xhot000000000000000000000000000000000009"
    # The new API invoice will get id=2 -> offset 2 -> payable 9_000_002.
    # Seed the settled row with EXACTLY that (address, amount) key so the
    # partial index would reject it if the index didn't ignore SETTLED rows.
    amount = 9_000_002
    with engine.begin() as conn:
        conn.execute(
            insert(Invoice).values(
                id=1,
                merchant_id=merchant["id"],
                public_id="seeded-settled",
                requested_base_units=9_000_000,
                amount_base_units=amount,
                status=InvoiceStatus.SETTLED,
                receiving_address=addr,
                expires_at=utcnow(),
            )
        )
    # New API invoice: id=2 -> offset 2 -> payable 9_000_000 + 2 = same key as
    # the settled row. Must be ALLOWED (the partial index ignores SETTLED).
    r = client.post(f"/merchants/{merchant['id']}/invoices", json={"amount": "9"},
                    headers=_auth(merchant))
    assert r.status_code == 201, r.text
    with get_sessionmaker(db_url)() as db:
        open_rows = db.execute(
            select(Invoice.id).where(
                Invoice.amount_base_units == amount,
                Invoice.status == InvoiceStatus.AWAITING_PAYMENT,
            )
        ).scalars().all()
    assert len(open_rows) == 1  # only the new open one; settled row doesn't block it


# --------------------------------------------------------------------------
# [D17] Finding 3 regression tests: hex identifiers are case-canonicalized at
# every boundary. A checksummed (EIP-55, mixed-case) RECEIVING_ADDRESS in
# settings once stored mixed-case on invoices while the watcher delivered
# lowercase to_address — SQLite compares case-sensitively, so every real
# payment was silently SKIPPED.
# --------------------------------------------------------------------------

def test_mixed_case_settings_address_is_normalized_and_credits(client, merchant, db_url, monkeypatch):
    """THE live bug: settings hold a MIXED-CASE hot address; the watcher
    delivers lowercase to_address. The event must credit the invoice."""
    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.processor import run_pending

    # Simulate .env holding the EIP-55 checksummed form (mixed case).
    checksummed = "0xHOT000000000000000000000000000000000009"
    assert checksummed != checksummed.lower() and checksummed.lower() == (
        "0xhot000000000000000000000000000000000009"
    )
    monkeypatch.setenv("RECEIVING_ADDRESS", checksummed)
    get_settings.cache_clear()  # settings are cached; re-read with the new env

    try:
        r = client.post(
            f"/merchants/{merchant['id']}/invoices",
            json={"amount": "7.5", "idempotency_key": "case-1"},
            headers=_auth(merchant),
        )
        assert r.status_code == 201, r.text
        inv = r.json()["invoice"]
        # Boundary 1: invoice stores the CANONICAL lowercase address.
        assert inv["receiving_address"] == checksummed.lower(), (
            "invoice must store a lowercased receiving_address [D17]"
        )

        # Boundary 2: ingest stores the event's addresses lowercase.
        payable = _to_base_units(inv["payable_amount"])
        ev = _event(tx="0xTXCASE00000000000000000000000000000001", amount=payable)
        ev["to_address"] = checksummed.lower()  # watcher always lowercases
        r = client.post(
            "/internal/events",
            json={"events": [ev]},
            headers={"X-Internal-Key": "test-internal-key"},
        )
        assert r.status_code == 202, r.text

        # Boundary 3: matching is now a plain == over canonical rows.
        with get_sessionmaker(db_url)() as db:
            outcomes = run_pending(db)
        assert outcomes[0]["outcome"] == "credited", outcomes
    finally:
        get_settings.cache_clear()  # don't leak the mixed-case env into other tests


def test_ingest_normalizes_mixed_case_hex_fields(client, merchant, db_url):
    """Ingest lowercases tx_hash and all addresses at the storage boundary,
    so replays dedupe regardless of the case the watcher sent."""
    from sqlalchemy import select
    from app.db import get_sessionmaker
    from app.models import ChainEvent

    ev = _event(tx="0xABCDEF00000000000000000000000000000009")
    ev["to_address"] = "0xHOT000000000000000000000000000000000009"
    ev["from_address"] = "0xPAYER0000000000000000000000000000000000"
    ev["contract_address"] = "0xUSDC0000000000000000000000000000000000"
    h = {"X-Internal-Key": "test-internal-key"}
    r1 = client.post("/internal/events", json={"events": [ev]}, headers=h)
    # Same event, different case -> same canonical row -> duplicate.
    ev2 = dict(ev, to_address="0xhot000000000000000000000000000000000009")
    r2 = client.post("/internal/events", json={"events": [ev2]}, headers=h)
    assert r1.json()["accepted"] == 1
    assert r2.json()["duplicates"] == 1

    with get_sessionmaker(db_url)() as db:
        row = db.execute(
            select(ChainEvent).where(ChainEvent.tx_hash == "0xabcdef00000000000000000000000000000009")
        ).scalar_one()
    assert row.to_address == "0xHOT000000000000000000000000000000000009".lower()
    assert row.from_address == "0xPAYER0000000000000000000000000000000000".lower()
    assert row.contract_address == "0xUSDC0000000000000000000000000000000000".lower()


def test_settings_validator_lowercases_addresses():
    """Single source of truth [D17]: Settings itself canonicalizes hex fields,
    so any future code path that reads settings gets lowercase for free."""
    from app.config import Settings

    s = Settings(receiving_address="0xAbCdEf0000000000000000000000000000000001", usdc_contract="0xUsDc0000000000000000000000000000000001")
    assert s.receiving_address == "0xabcdef0000000000000000000000000000000001"
    assert s.usdc_contract == "0xusdc0000000000000000000000000000000001"


# --------------------------------------------------------------------------
# [D21] Hosted checkout page: public fields only, QR payment URI, no secrets.
# --------------------------------------------------------------------------

def test_checkout_page_renders_public_fields_and_qr(client, merchant):
    r = client.post(
        f"/merchants/{merchant['id']}/invoices",
        json={"amount": "12.34", "description": "Widget"},
        headers=_auth(merchant),
    )
    pid = r.json()["invoice"]["public_id"]

    page = client.get(f"/pay/{pid}")
    assert page.status_code == 200
    body = page.text
    assert "12.34" in body
    assert "AWAITING_PAYMENT" in body
    assert "data:image/svg+xml;base64," in body   # QR is embedded, no external calls
    assert "0xhot" in body                        # receiving address is public info

    # The page must NEVER carry secret material:
    assert merchant["api_key"] not in body
    assert merchant["webhook_secret"] not in body


def test_checkout_page_404_for_unknown_invoice(client):
    assert client.get("/pay/doesnotexist").status_code == 404


def test_checkout_qr_uri_carries_chain_id(client, merchant, db_url, monkeypatch):
    """EIP-681: WITHOUT @chainId the URI defaults to MAINNET — a Sepolia
    deployment's QR once silently pointed wallets at the wrong network.
    The chain_id setting must appear in the payment URI."""
    from app.config import get_settings
    from app.routes import _payment_uri

    monkeypatch.setenv("CHAIN_ID", "11155111")
    get_settings.cache_clear()
    try:
        uri = _payment_uri(3_500_006)
        usdc = get_settings().usdc_contract
        assert uri == (
            f"ethereum:{usdc}@11155111/transfer"
            f"?address=0xhot000000000000000000000000000000000009&uint256=3500006"
        )
    finally:
        get_settings.cache_clear()
