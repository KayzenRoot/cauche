"""CD-27: remaining Kalshi instruction paths; exclusively local mock transport."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.integrations import Kalshi, VenueError
from app.integrations import common, kalshi

NOW = 1800000000
TICKER = "FIXTURE"
EVENT = "FIXTURE-EVENT"
ROOT = "/trade-api/v2"
ORDER = "/portfolio/events/orders"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(kalshi, "time", Mock(time=lambda: NOW, time_ns=lambda: NOW * 10**9))


@pytest.fixture(scope="module")
def pem():
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def market(**overrides):
    return {
        "ticker": TICKER, "event_ticker": EVENT, "market_type": "binary",
        "status": "active", "close_time": "2099-12-31T00:00:00Z",
        "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}], **overrides,
    }


def ack(**overrides):
    return {
        "order_id": "order-1", "client_order_id": "fixture-intent",
        "fill_count": "2", "remaining_count": "0", "average_fill_price": "0.40",
        "average_fee_paid": "0.001", "ts_ms": NOW * 1000, **overrides,
    }


def adapter(pem, responses):
    calls = []

    def respond(request):
        assert request.url.host == "external-api.demo.kalshi.co"
        path = request.url.path.removeprefix(ROOT)
        calls.append((request.method, path, json.loads(request.content) if request.content else None))
        if request.method == "GET":
            assert "KALSHI-ACCESS-KEY" not in request.headers
        else:
            assert request.headers["KALSHI-ACCESS-KEY"] == "fixture-key"
        return httpx.Response(200, json=responses[(request.method, path)])

    return Kalshi("fixture-key", pem, _transport=httpx.MockTransport(respond)), calls


async def submit(venue, side="YES", price="0.42", quantity="2", *, post_only=False):
    return await venue.submit_demo(
        TICKER, side, Decimal(price), Decimal(quantity),
        client_order_id="fixture-intent", post_only=post_only,
    )


@pytest.mark.parametrize("side,price,quantity", [
    ("OTHER", "0.42", "2"), ("YES", "0", "2"),
    ("YES", "0.42", "0"), ("YES", "0.42", "10001"),
])
async def test_invalid_order_never_touches_transport(pem, side, price, quantity):
    venue, calls = adapter(pem, {})
    with pytest.raises(VenueError, match="^INVALID_ORDER$"):
        await submit(venue, side, price, quantity)
    assert calls == []


@pytest.mark.parametrize("price,quantity", [("0.42001", "2"), ("0.42", "2.001")])
async def test_invalid_precision_never_touches_transport(pem, price, quantity):
    venue, calls = adapter(pem, {})
    with pytest.raises(VenueError, match="^INVALID_PRECISION$"):
        await submit(venue, price=price, quantity=quantity)
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"market_type": "scalar"}, {"status": "closed"}, {"close_time": "2020-01-01T00:00:00Z"},
])
async def test_untradable_market_stops_before_write(pem, changes):
    venue, calls = adapter(pem, {("GET", f"/markets/{TICKER}"): {"market": market(**changes)}})
    with pytest.raises(VenueError, match="^MARKET_NOT_TRADABLE$"):
        await submit(venue)
    assert [method for method, _, _ in calls] == ["GET"]


async def test_mock_post_only_expiration_and_resting_ack(pem):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market()},
        ("POST", ORDER): ack(fill_count="0", remaining_count="2"),
    })
    result = await submit(venue, post_only=True)
    assert result["status"] == "OPEN" and result["filled_quantity"] == 0
    assert result["average_fill_price"] is None and result["fee"] == 0
    assert [method for method, _, _ in calls] == ["GET", "POST"]
    payload = calls[-1][2]
    assert payload["expiration_time"] == NOW + 45
    assert payload["post_only"] is True and payload["time_in_force"] == "good_till_canceled"


async def test_mock_complete_fill_reports_exact_quantity_and_fee(pem):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market()}, ("POST", ORDER): ack(),
    })
    result = await submit(venue)
    assert result["status"] == "FILLED"
    assert result["filled_quantity"] == Decimal("2") and result["remaining_quantity"] == 0
    assert result["average_fill_price"] == Decimal("0.40") and result["fee"] == Decimal("0.002")
    assert [method for method, _, _ in calls] == ["GET", "POST"]


@pytest.mark.parametrize("changes", [
    {"client_order_id": "other-intent"}, {"fill_count": "3"}, {"fill_count": "1", "remaining_count": "1"},
])
async def test_mock_ambiguous_ack_does_not_retry(pem, changes):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market()}, ("POST", ORDER): ack(**changes),
    })
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$") as error:
        await submit(venue)
    assert error.value.outcome_unknown
    assert [method for method, _, _ in calls] == ["GET", "POST"]


@pytest.mark.parametrize("changes", [{"average_fill_price": "0.43"}, {"average_fee_paid": "-0.001"}])
async def test_mock_quote_divergence_does_not_retry(pem, changes):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market()}, ("POST", ORDER): ack(**changes),
    })
    with pytest.raises(VenueError, match="^BROKER_QUOTE_DIVERGENCE$") as error:
        await submit(venue)
    assert error.value.outcome_unknown
    assert [method for method, _, _ in calls] == ["GET", "POST"]


@pytest.mark.parametrize("changes", [{"order_id": "other"}, {"reduced_by": "-1"}])
async def test_mock_cancel_ambiguous_response_does_not_retry(pem, changes):
    path = f"{ORDER}/order-1"
    venue, calls = adapter(pem, {("DELETE", path): {
        "order_id": "order-1", "reduced_by": "1", "ts_ms": NOW * 1000, **changes,
    }})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$") as error:
        await venue.cancel_demo("order-1", TICKER)
    assert error.value.outcome_unknown
    assert [(method, path) for method, path, _ in calls] == [("DELETE", path)]


@pytest.mark.parametrize("grid", [
    {"start": "0", "end": "1", "step": "0"},
    {"start": "0.5", "end": "0.4", "step": "0.01"},
    {"start": "0.4", "end": "0.5", "step": "0.2"},
])
async def test_market_price_grid_rejects_invalid_intervals(pem, grid):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market(price_ranges=[grid])},
    })
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.market(TICKER, include_fees=False)
    assert len(calls) == 1


async def test_market_invalid_iso_timestamp_fails_closed(pem):
    venue, calls = adapter(pem, {
        ("GET", f"/markets/{TICKER}"): {"market": market(close_time="not-a-timestamp")},
    })
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.market(TICKER, include_fees=False)
    assert len(calls) == 1


async def test_oversized_exclusive_event_returns_no_advisory_candidates(pem):
    path = f"/events/{EVENT}"
    venue, calls = adapter(pem, {("GET", path): {"event": {
        "event_ticker": EVENT, "mutually_exclusive": True, "collateral_return_type": "MECNET",
        "settlement_sources": [{"name": "fixture"}], "markets": [market()] * 101,
    }}})
    assert await venue.exclusive_siblings({"event_ticker": EVENT}) == []
    assert [(method, path) for method, path, _ in calls] == [("GET", path)]
