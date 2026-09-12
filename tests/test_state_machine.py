"""State machine unit tests: the lifecycle table is the contract [D11]."""
from __future__ import annotations

import pytest

from app.models import Invoice, InvoiceStatus
from app.state_machine import IllegalTransition, transition


def inv(status: InvoiceStatus) -> Invoice:
    return Invoice(status=status)  # not persisted — transition() is pure


def test_happy_path_chain():
    i = inv(InvoiceStatus.CREATED)
    transition(i, InvoiceStatus.AWAITING_PAYMENT)
    transition(i, InvoiceStatus.DETECTED)
    transition(i, InvoiceStatus.CONFIRMING)
    transition(i, InvoiceStatus.CONFIRMED)
    transition(i, InvoiceStatus.SETTLED)
    assert i.status is InvoiceStatus.SETTLED


def test_illegal_skips_are_loud():
    i = inv(InvoiceStatus.AWAITING_PAYMENT)
    with pytest.raises(IllegalTransition):
        transition(i, InvoiceStatus.CONFIRMED)  # cannot skip DETECTED/CONFIRMING


def test_terminal_states_have_no_exits():
    for terminal in (InvoiceStatus.SETTLED, InvoiceStatus.EXPIRED):
        with pytest.raises(IllegalTransition):
            transition(inv(terminal), InvoiceStatus.AWAITING_PAYMENT)


def test_same_state_is_idempotent_noop():
    i = inv(InvoiceStatus.DETECTED)
    transition(i, InvoiceStatus.DETECTED)  # replayed transition: fine
    assert i.status is InvoiceStatus.DETECTED
