"""CD-05: Deriv settlement invariants with a mocked read-only venue."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import demo_execution as execution


NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
EPOCH = int(NOW.timestamp())


@pytest.fixture
def boundary(monkeypatch):
    order = SimpleNamespace(
        id="order-fixture", broker_id="123", mode="BROKER_DEMO", exchange="DERIV",
        source="OFFICIAL_DEMO", currency="USD", status="OPEN",
        quantity=Decimal("1"), price=Decimal("1"), timestamp=NOW,
    )
    position = SimpleNamespace(status="OPEN", currency="USD", quantity=Decimal("1"), exposure=Decimal("1"))
    result = dict(broker_id="123", source="DERIV_DEMO", currency="USD", status="SETTLED",
                  price=Decimal("1"), payout=Decimal("1.80"), profit=Decimal("0.70"),
                  fee=Decimal("0.10"), settled_at=EPOCH + 300, observed_at=EPOCH + 301)
    db = Mock()
    db.get.return_value = position
    db.scalars.return_value = [Decimal("0.10")]
    venue = SimpleNamespace(reconcile_demo=AsyncMock(return_value=result), submit_demo=AsyncMock())
    settle = Mock()
    monkeypatch.setattr(execution, "record_settlement", settle)
    return SimpleNamespace(db=db, order=order, position=position, result=result, venue=venue, settle=settle)


@pytest.mark.parametrize("payout,profit", [("1.80", "0.70"), ("0", "-1.10"), ("1", "-0.10")])
async def test_exact_profit_and_fee_once(boundary, payout, profit):
    c = boundary
    c.result.update(payout=payout, profit=profit)
    await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_called_once_with(
        c.db, c.order, gross=Decimal(payout) - Decimal("1"), payout=Decimal(payout),
        timestamp=execution.moment(EPOCH + 300),
    )
    c.venue.reconcile_demo.assert_awaited_once_with("123")
    c.venue.submit_demo.assert_not_awaited()


async def test_open_does_not_settle(boundary):
    c = boundary
    c.result.update(status="OPEN", profit=None, settled_at=None)
    await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_not_called()
    c.db.get.assert_not_called()


async def test_already_settled_does_not_lookup(boundary):
    c = boundary
    c.order.status = "SETTLED"
    await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.venue.reconcile_demo.assert_not_awaited()
    c.settle.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("mode", "PAPER"), ("exchange", "KALSHI"), ("source", "SIMULATED_FIXTURE"),
    ("currency", "EUR"), ("broker_id", None),
])
async def test_non_demo_order_never_queries_venue(boundary, field, value):
    c = boundary
    setattr(c.order, field, value)
    with pytest.raises(ValueError, match="INVALID_BROKER_SETTLEMENT"):
        await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.venue.reconcile_demo.assert_not_awaited()
    c.settle.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("broker_id", "456"), ("source", "DERIV_REAL"), ("status", "UNKNOWN"),
    ("status", []), ("currency", "EUR"), ("price", None), ("price", "1.01"),
    ("payout", None), ("payout", "NaN"), ("payout", "-1"), ("payout", "0.123456789"),
    ("profit", None), ("profit", "Infinity"), ("profit", "0.80"), ("profit", "-100"),
    ("fee", "-1"), ("fee", "0.11"), ("fee", "NaN"),
    ("settled_at", None), ("settled_at", EPOCH - 1), ("settled_at", True),
    ("settled_at", EPOCH + 302), ("observed_at", None), ("observed_at", 10**30),
])
async def test_invalid_settlement_does_not_mutate_ledger(boundary, field, value):
    c = boundary
    c.result[field] = value
    with pytest.raises(ValueError, match="INVALID_BROKER_(SETTLEMENT|CONFIRMATION)|UNSUPPORTED_CURRENCY"):
        await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_not_called()
    c.venue.submit_demo.assert_not_awaited()
    assert c.order.status == c.position.status == "OPEN"


@pytest.mark.parametrize("field,value", [
    ("status", "SETTLED"), ("currency", "EUR"), ("quantity", Decimal("2")),
    ("exposure", Decimal("1.01")),
])
async def test_position_divergence_blocks_settlement(boundary, field, value):
    c = boundary
    setattr(c.position, field, value)
    with pytest.raises(ValueError, match="INVALID_BROKER_SETTLEMENT"):
        await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_not_called()


async def test_missing_position_blocks_settlement(boundary):
    c = boundary
    c.db.get.return_value = None
    with pytest.raises(ValueError, match="INVALID_BROKER_SETTLEMENT"):
        await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_not_called()


async def test_missing_optional_reported_fee_uses_booked_fee(boundary):
    c = boundary
    c.result["fee"] = None
    await execution.reconcile_deriv(c.db, c.order, c.venue)
    c.settle.assert_called_once_with(
        c.db, c.order, gross=Decimal("0.80"), payout=Decimal("1.80"),
        timestamp=execution.moment(EPOCH + 300),
    )
