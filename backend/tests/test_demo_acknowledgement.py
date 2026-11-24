"""CAU-MVP-001/CD-01: acknowledgement boundary, no broker network."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import demo_execution as execution
from app.domain import Decision


@pytest.fixture
def boundary(monkeypatch):
    db = Mock()
    order = SimpleNamespace(id="order-fixture")
    create = Mock(return_value=order)
    fill = Mock()
    emit = Mock()
    monkeypatch.setattr(execution, "new_order", create)
    monkeypatch.setattr(execution, "record_fill", fill)
    monkeypatch.setattr(execution, "emit", emit)
    return db, order, create, fill, emit


def intent_for(exchange):
    decision = Decision(
        strategy=f"{exchange}-STRAT-01", version="1", reason="TEST",
        side="CALL" if exchange == "DERIV" else "YES",
        price=Decimal("1") if exchange == "DERIV" else Decimal("0.40"),
        quantity=Decimal("1"), orderable=True,
    )
    return SimpleNamespace(
        id="intent-fixture", exchange=exchange, instrument="TEST",
        request={"decision": decision.as_dict()}, status="SUBMITTING", response={},
    )


def normalized_ack(exchange, status="OPEN"):
    intent = intent_for(exchange)
    if exchange == "DERIV":
        return {
            "broker_id": "123", "status": status, "currency": "USD",
            "price": Decimal("1"), "quantity": Decimal("1"),
            "source": "DERIV_DEMO", "client_order_id": intent.id,
        }
    filled, remaining = {
        "OPEN": ("0", "1"), "ACCEPTED": ("0", "1"), "PARTIAL": ("0.25", "0.75"),
        "FILLED": ("1", "0"), "canceled": ("0", "0"), "CANCELLED": ("0", "0"),
    }[status]
    return {
        "broker_id": "123", "status": status, "price": Decimal("0.40"),
        "quantity": Decimal("1"), "filled_quantity": Decimal(filled),
        "remaining_quantity": Decimal(remaining), "fee": Decimal("0"),
        "source": "KALSHI_DEMO", "client_order_id": intent.id,
        "instrument": intent.instrument, "side": "YES",
    }


@pytest.mark.parametrize("status", ["UNKNOWN", "PENDING", "", None, 1, "OPEN "])
@pytest.mark.parametrize("exchange", ["DERIV", "KALSHI"])
def test_unknown_status_never_creates_order_or_fill(boundary, exchange, status):
    db, _, create, fill, emit = boundary
    intent = intent_for(exchange)
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, {"broker_id": "123", "status": status})
    assert intent.status == "SUBMITTING"
    assert intent.response == {}
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()


@pytest.mark.parametrize("broker_id", [None, "", " ", " 123", "123 ", 123, "x" * 101])
def test_invalid_broker_identity_is_not_coerced(boundary, broker_id):
    db, _, create, fill, emit = boundary
    intent = intent_for("KALSHI")
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, {"broker_id": broker_id, "status": "OPEN"})
    assert intent.status == "SUBMITTING"
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED", "EXPIRED", "PARTIAL", "FILLED", "ACCEPTED"])
def test_deriv_non_open_never_records_fill(boundary, status):
    db, _, create, fill, emit = boundary
    intent = intent_for("DERIV")
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, {"broker_id": "123", "status": status, "currency": "USD"})
    assert intent.status == "SUBMITTING"
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()


def test_valid_deriv_acknowledgement_records_exact_fill(boundary):
    db, order, create, fill, _ = boundary
    intent = intent_for("DERIV")
    result = normalized_ack("DERIV")
    assert execution.acknowledge(db, intent, result) is order
    assert intent.status == "CONFIRMED"
    assert create.call_args.kwargs["status"] == "OPEN"
    fill.assert_called_once()
    assert fill.call_args.kwargs["quantity"] == Decimal("1")
    assert fill.call_args.kwargs["price"] == Decimal("1")


@pytest.mark.parametrize("status, expected", [
    ("OPEN", "OPEN"), ("PARTIAL", "PARTIAL"), ("FILLED", "FILLED"), ("canceled", "CANCELLED"),
])
def test_valid_kalshi_acknowledgement_defers_fill_to_reconciliation(boundary, status, expected):
    db, order, create, fill, _ = boundary
    intent = intent_for("KALSHI")
    assert execution.acknowledge(db, intent, normalized_ack("KALSHI", status)) is order
    assert intent.status == "CONFIRMED"
    assert create.call_args.kwargs["status"] == expected
    fill.assert_not_called()


async def test_recovery_unknown_status_does_not_confirm_or_resubmit(boundary, monkeypatch):
    db, _, create, fill, _ = boundary
    intent = intent_for("KALSHI")
    bot = SimpleNamespace(state="RUNNING", desired_state="RUNNING")
    venue = SimpleNamespace(
        find_demo_order=AsyncMock(return_value={"broker_id": "123", "status": "UNKNOWN"}),
        submit_demo=AsyncMock(),
    )
    monkeypatch.setattr(execution, "Kalshi", type(venue))
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        await execution.recover_claim(db, bot, venue, intent)
    assert intent.status == "UNKNOWN"
    assert bot.desired_state == "PAUSED"
    assert bot.state == "RISK_BLOCKED"
    venue.find_demo_order.assert_awaited_once_with(intent.id, intent.instrument)
    venue.submit_demo.assert_not_awaited()
    create.assert_not_called()
    fill.assert_not_called()


# CAU-MVP-001/CD-02: malformed normalized monetary/identity payloads.
@pytest.mark.parametrize("exchange, field, value", [
    ("DERIV", "price", None),
    ("DERIV", "price", "NaN"),
    ("DERIV", "price", "sNaN"),
    ("DERIV", "price", "Infinity"),
    ("DERIV", "price", "garbage"),
    ("DERIV", "price", True),
    ("DERIV", "price", 0.5),
    ("DERIV", "price", "0"),
    ("DERIV", "price", "-1"),
    ("DERIV", "price", "1.01"),
    ("DERIV", "price", "0.123456789"),
    ("DERIV", "price", "1e1000"),
    ("DERIV", "quantity", "2"),
    ("DERIV", "quantity", None),
    ("DERIV", "filled_quantity", "0"),
    ("DERIV", "remaining_quantity", "0.5"),
    ("DERIV", "fee", "-0.01"),
    ("DERIV", "fee", "NaN"),
    ("DERIV", "fee", None),
    ("DERIV", "source", "DERIV_REAL"),
    ("DERIV", "client_order_id", "different-intent"),
    ("KALSHI", "price", None),
    ("KALSHI", "price", "0.41"),
    ("KALSHI", "quantity", "0.5"),
    ("KALSHI", "filled_quantity", "-0.1"),
    ("KALSHI", "remaining_quantity", "1.1"),
    ("KALSHI", "remaining_quantity", None),
    ("KALSHI", "filled_quantity", "0.2"),
    ("KALSHI", "remaining_quantity", "0"),
    ("KALSHI", "fee", "-1"),
    ("KALSHI", "fee", "Infinity"),
    ("KALSHI", "fee", None),
    ("KALSHI", "instrument", "OTHER"),
    ("KALSHI", "side", "NO"),
    ("KALSHI", "client_order_id", "different-intent"),
    ("KALSHI", "source", "KALSHI_REAL"),
    ("KALSHI", "status", "FILLED"),
    ("KALSHI", "status", "PARTIAL"),
    ("KALSHI", "status", "CANCELLED"),
])
def test_invalid_normalized_confirmation_has_no_accounting_effect(boundary, exchange, field, value):
    db, _, create, fill, emit = boundary
    intent = intent_for(exchange)
    result = normalized_ack(exchange)
    result[field] = value
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, result)
    assert intent.status == "SUBMITTING"
    assert intent.response == {}
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_non_mapping_acknowledgement_is_rejected(boundary, value):
    db, _, create, fill, emit = boundary
    intent = intent_for("DERIV")
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, value)
    assert intent.status == "SUBMITTING"
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()


def test_deriv_exact_amounts_and_actual_fee(boundary):
    db, order, create, fill, _ = boundary
    intent = intent_for("DERIV")
    result = normalized_ack("DERIV")
    result.update(price="0.95", fee="0.01")
    assert execution.acknowledge(db, intent, result) is order
    assert create.call_args.kwargs["price"] == Decimal("0.95")
    assert create.call_args.kwargs["meta"]["reported_filled"] == "1"
    assert fill.call_args.kwargs["fee"] == Decimal("0.01")
    assert fill.call_args.kwargs["quantity"] == Decimal("1")


def test_kalshi_partial_ioc_with_cancelled_remainder(boundary):
    db, order, create, fill, _ = boundary
    intent = intent_for("KALSHI")
    result = normalized_ack("KALSHI", "PARTIAL")
    result["remaining_quantity"] = Decimal("0")
    assert execution.acknowledge(db, intent, result) is order
    assert create.call_args.kwargs["remaining"] == Decimal("0")
    assert create.call_args.kwargs["meta"]["reported_filled"] == "0.25"
    fill.assert_not_called()


def test_kalshi_partial_exceeding_total_is_rejected(boundary):
    db, _, create, fill, emit = boundary
    intent = intent_for("KALSHI")
    result = normalized_ack("KALSHI", "PARTIAL")
    result["remaining_quantity"] = Decimal("0.80")
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        execution.acknowledge(db, intent, result)
    assert intent.status == "SUBMITTING"
    create.assert_not_called()
    fill.assert_not_called()
    emit.assert_not_called()
