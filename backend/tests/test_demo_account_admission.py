"""CD-10: demo account admission and exact balance, no broker I/O."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import demo_execution as execution
from app.integrations import Deriv, Kalshi, VenueError


@pytest.mark.parametrize("exchange,credentials", [
    ("DERIV", {"app_id": "fixture", "token": "fixture"}),
    ("DERIV", {"app_id": "fixture", "token": "fixture", "account_id": "fixture"}),
    ("KALSHI", {"key_id": "fixture", "private_key": "fixture"}),
])
def test_client_constructs_only_expected_adapter(monkeypatch, exchange, credentials):
    decrypt = Mock(return_value=credentials)
    monkeypatch.setattr(execution, "decrypt_credentials", decrypt)
    connection = SimpleNamespace(enabled=True, ciphertext="fixture", exchange=exchange)
    result = execution.client(connection)
    assert isinstance(result, Deriv if exchange == "DERIV" else Kalshi)
    if exchange == "KALSHI":
        assert result.source == "KALSHI_DEMO"
    decrypt.assert_called_once_with("fixture")


@pytest.mark.parametrize("enabled,ciphertext,exchange,error", [
    (False, "fixture", "DERIV", "DEMO_NOT_CONFIGURED"),
    (True, None, "KALSHI", "DEMO_NOT_CONFIGURED"),
    (True, "fixture", "OTHER", "UNSUPPORTED_EXCHANGE"),
])
def test_client_invalid_connection_does_not_decrypt(monkeypatch, enabled, ciphertext, exchange, error):
    decrypt = Mock(side_effect=AssertionError("must not decrypt"))
    monkeypatch.setattr(execution, "decrypt_credentials", decrypt)
    with pytest.raises(ValueError, match=error):
        execution.client(SimpleNamespace(enabled=enabled, ciphertext=ciphertext, exchange=exchange))
    decrypt.assert_not_called()


@pytest.mark.parametrize("exchange,credentials", [
    ("DERIV", {}),
    ("DERIV", {"app_id": "fixture", "token": " "}),
    ("DERIV", {"app_id": "fixture", "token": "fixture", "url": "fixture"}),
    ("DERIV", {"app_id": "fixture", "token": "fixture", "account_id": None}),
    ("KALSHI", {"key_id": "fixture"}),
    ("KALSHI", {"key_id": "fixture", "private_key": 1}),
    ("KALSHI", {"key_id": "fixture", "private_key": "fixture", "public": "true"}),
    ("KALSHI", []),
])
def test_client_malformed_credentials_fail_closed(monkeypatch, exchange, credentials):
    monkeypatch.setattr(execution, "decrypt_credentials", Mock(return_value=credentials))
    with pytest.raises(ValueError, match="CREDENTIALS_UNAVAILABLE"):
        execution.client(SimpleNamespace(enabled=True, ciphertext="fixture", exchange=exchange))


@pytest.mark.parametrize("portfolio,expected", [([], False), ([{"broker_id": "fixture"}], True)])
async def test_deriv_inventory(portfolio, expected):
    venue = Mock(spec=Deriv)
    venue.portfolio = AsyncMock(return_value=portfolio)
    assert await execution.has_unmanaged_exposure(venue) is expected
    venue.portfolio.assert_awaited_once_with()


@pytest.mark.parametrize("portfolio", [None, {}, "", False])
async def test_deriv_malformed_inventory_is_not_empty(portfolio):
    venue = Mock(spec=Deriv)
    venue.portfolio = AsyncMock(return_value=portfolio)
    with pytest.raises(ValueError, match="INVALID_DEMO_INVENTORY"):
        await execution.has_unmanaged_exposure(venue)


@pytest.mark.parametrize("quantity,exposure,orders,expected", [
    ("0", "0", [], False),
    ("0", "0", [{"broker_id": "fixture"}], True),
    ("-1", "0", [], True),
    ("0", "0.01", [], True),
    ("0", "-0.01", [], True),
])
async def test_kalshi_inventory(quantity, exposure, orders, expected):
    venue = Mock(spec=Kalshi)
    venue.positions = AsyncMock(return_value=[{"quantity": quantity, "exposure": exposure}])
    venue.open_orders = AsyncMock(return_value=orders)
    assert await execution.has_unmanaged_exposure(venue) is expected
    if Decimal(quantity) or Decimal(exposure):
        venue.open_orders.assert_not_awaited()
    else:
        venue.open_orders.assert_awaited_once_with()


@pytest.mark.parametrize("positions", [
    None, {}, "", [None], [{}], [{"quantity": False, "exposure": "0"}],
    [{"quantity": "NaN", "exposure": "0"}], [{"quantity": "0", "exposure": "Infinity"}],
])
async def test_kalshi_malformed_positions_never_permit_admission(positions):
    venue = Mock(spec=Kalshi)
    venue.positions = AsyncMock(return_value=positions)
    venue.open_orders = AsyncMock()
    with pytest.raises(ValueError, match="INVALID_DEMO_INVENTORY|INVALID_BROKER_CONFIRMATION"):
        await execution.has_unmanaged_exposure(venue)
    venue.open_orders.assert_not_awaited()


@pytest.mark.parametrize("orders", [None, {}, "", False])
async def test_kalshi_malformed_orders_never_permit_admission(orders):
    venue = Mock(spec=Kalshi)
    venue.positions = AsyncMock(return_value=[])
    venue.open_orders = AsyncMock(return_value=orders)
    with pytest.raises(ValueError, match="INVALID_DEMO_INVENTORY"):
        await execution.has_unmanaged_exposure(venue)


async def test_unknown_venue_never_queries_inventory():
    venue = Mock()
    with pytest.raises(ValueError, match="UNSUPPORTED_EXCHANGE"):
        await execution.has_unmanaged_exposure(venue)
    assert not venue.mock_calls


@pytest.mark.parametrize("exchange", ["DERIV", "KALSHI"])
async def test_inventory_transport_failure_propagates(exchange):
    venue = Mock(spec=Deriv if exchange == "DERIV" else Kalshi)
    method = "portfolio" if exchange == "DERIV" else "positions"
    setattr(venue, method, AsyncMock(side_effect=VenueError("MOCK_TIMEOUT", retryable=True)))
    with pytest.raises(VenueError, match="MOCK_TIMEOUT"):
        await execution.has_unmanaged_exposure(venue)


@pytest.mark.parametrize("exchange,balance", [("DERIV", "0"), ("KALSHI", "100.12345678")])
def test_verified_balance_preserves_exact_decimal(exchange, balance):
    assert execution.verified_demo_balance(exchange, {
        "environment": "BROKER_DEMO", "source": f"{exchange}_DEMO", "currency": "USD", "balance": balance,
    }) == Decimal(balance)


@pytest.mark.parametrize("field,value", [
    ("balance", None), ("balance", True), ("balance", 1.0), ("balance", "-1"),
    ("balance", "NaN"), ("balance", "Infinity"), ("balance", "1e16"), ("balance", "1.123456789"),
    ("currency", "EUR"), ("environment", "REAL"), ("source", "KALSHI_PUBLIC"),
])
def test_invalid_balance_fails_closed(field, value):
    payload = {"environment": "BROKER_DEMO", "source": "KALSHI_DEMO", "currency": "USD", "balance": "100"}
    payload[field] = value
    with pytest.raises(ValueError, match="INVALID_DEMO_BALANCE|UNSUPPORTED_CURRENCY|INVALID_BROKER_CONFIRMATION"):
        execution.verified_demo_balance("KALSHI", payload)


@pytest.mark.parametrize("exchange,payload", [("OTHER", {}), ("KALSHI", None), ("DERIV", [])])
def test_malformed_balance_envelope(exchange, payload):
    with pytest.raises(ValueError, match="INVALID_DEMO_BALANCE"):
        execution.verified_demo_balance(exchange, payload)
