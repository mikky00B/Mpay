"""Money helpers [D4].

USDC uses 6 decimals. All internal math is integer base units; the API layer
converts decimal strings exactly via Decimal — floats never touch money.
"""
from decimal import Decimal, InvalidOperation

USDC_DECIMALS = 6
_SCALE = 10**USDC_DECIMALS


def parse_amount_to_base_units(amount: str) -> int:
    """Parse a decimal string like '25.50' into base units (25500000)."""
    try:
        d = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {amount!r}") from exc
    if d <= 0:
        raise ValueError("amount must be positive")
    units = d * _SCALE
    if units != units.to_integral_value():
        raise ValueError(f"more than {USDC_DECIMALS} decimals not supported")
    return int(units)


def base_units_to_decimal_string(base_units: int) -> str:
    """Render base units as a decimal string (25500000 -> '25.5')."""
    d = Decimal(base_units) / _SCALE
    return format(d.normalize(), "f")


def unique_offset_for_invoice(invoice_id: int) -> int:
    """Per-invoice minor-unit offset making (address, amount) unique [D6][D15].

    The offset IS the invoice id: uniqueness holds for the lifetime of the
    table (the old `id % 900 + 1` scheme wrapped every 900 invoices, so two
    simultaneously open invoices could collide on (address, amount) and a
    payment could be credited to the wrong one).

    Trade-off [D15]: the offset grows unboundedly with the id instead of being
    capped sub-cent. At 6 decimals, invoice id N shifts the payable amount by
    N/1,000,000 USDC — an id in the millions is still a rounding-level bump on
    a real invoice, and correctness of payment matching outweighs the cosmetic
    sub-cent cap. If amounts must stay visually exact, switch to per-invoice
    derived addresses (HD route in plan.md) instead.
    """
    return invoice_id
