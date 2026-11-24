"""CD-23: Kalshi authenticated boundary and pagination with isolated transport."""

import base64
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from app.integrations import Kalshi, VenueError
from app.integrations import common

ROOT = "/trade-api/v2"
DEMO = "external-api.demo.kalshi.co"
PUBLIC = "external-api.kalshi.com"


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())


def pem(key, encryption=None):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        encryption if encryption is not None else serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(scope="module", params=["rsa", "ed25519"])
def signer(request):
    key = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
           if request.param == "rsa" else ed25519.Ed25519PrivateKey.generate())
    return pem(key), key.public_key()


@pytest.fixture(scope="module")
def demo_pem():
    return pem(ed25519.Ed25519PrivateKey.generate())


def verify_signature(request, public_key):
    timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
    signature = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"], validate=True)
    assert timestamp.isdecimal()
    assert request.headers["KALSHI-ACCESS-KEY"] == "fixture-key"
    message = f"{timestamp}{request.method}{request.url.path}".encode()
    if isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(signature, message, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
    else:
        public_key.verify(signature, message)


@pytest.mark.parametrize("credentials,error", [
    ((None, "fixture"), "NOT_CONFIGURED"), (("fixture", None), "NOT_CONFIGURED"),
    (("", ""), "NOT_CONFIGURED"), (("bad/key", "fixture"), "INVALID_IDENTIFIER"),
    (("fixture", "not-a-pem"), "AUTH_ERROR"), (("fixture", "é"), "AUTH_ERROR"),
    (("fixture", b"not-a-pem"), "AUTH_ERROR"), (("fixture", 1), "AUTH_ERROR"),
])
async def test_invalid_credentials_do_not_reach_transport(credentials, error):
    transport = Mock(side_effect=AssertionError("invalid credentials must not reach transport"))
    venue = Kalshi(*credentials, _transport=httpx.MockTransport(transport))
    with pytest.raises(VenueError, match=f"^{error}$"):
        await venue.verify_demo()
    transport.assert_not_called()


@pytest.mark.parametrize("kind", ["unsupported", "encrypted"])
async def test_invalid_key_type_or_encrypted_key_fails_closed(kind):
    key = ec.generate_private_key(ec.SECP256R1()) if kind == "unsupported" else ed25519.Ed25519PrivateKey.generate()
    encryption = serialization.BestAvailableEncryption(b"local-fixture") if kind == "encrypted" else None
    transport = Mock(side_effect=AssertionError("invalid key must not reach transport"))
    venue = Kalshi("fixture-key", pem(key, encryption), _transport=httpx.MockTransport(transport))
    with pytest.raises(VenueError, match="^AUTH_ERROR$"):
        await venue.verify_demo()
    transport.assert_not_called()


@pytest.mark.parametrize("operation", ["balance", "positions", "orders", "settlements"])
async def test_public_mode_cannot_read_authenticated_inventory(operation, demo_pem):
    transport = Mock(side_effect=AssertionError("public mode must not access portfolio"))
    venue = Kalshi("fixture-key", demo_pem, public=True, _transport=httpx.MockTransport(transport))
    actions = {"balance": venue.verify_demo, "positions": venue.positions, "orders": venue.open_orders,
               "settlements": lambda: venue.settlements("FIXTURE")}
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        await actions[operation]()
    transport.assert_not_called()


def position(ticker="FIXTURE", **overrides):
    return {"ticker": ticker, "position_fp": "-1.25", "market_exposure_dollars": "0.50",
            "realized_pnl_dollars": "-0.01", "fees_paid_dollars": "0.0012", **overrides}


async def test_signed_demo_positions_preserve_values_and_cursor(signer):
    private, public = signer
    calls = []

    def respond(request):
        assert request.method == "GET" and request.url.host == DEMO
        assert request.url.path == ROOT + "/portfolio/positions"
        verify_signature(request, public)
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        assert request.url.params["limit"] == "200"
        if cursor is None:
            return httpx.Response(200, json={"market_positions": [position()], "cursor": "fixture+/="})
        assert cursor == "fixture+/="
        return httpx.Response(200, json={"market_positions": [position("OTHER", position_fp="0")], "cursor": ""})

    venue = Kalshi("fixture-key", private, _transport=httpx.MockTransport(respond))
    result = await venue.positions()
    assert calls == [None, "fixture+/="]
    assert [row["instrument"] for row in result] == ["FIXTURE", "OTHER"]
    assert result[0] == {"instrument": "FIXTURE", "quantity": Decimal("-1.25"),
                         "exposure": Decimal("0.50"), "realized_pnl": Decimal("-0.01"),
                         "fee": Decimal("0.0012"), "observed_at": None}
    assert result[1]["quantity"] == Decimal(0)


@pytest.mark.parametrize("payload", [
    {"market_positions": []},
    {"market_positions": [], "cursor": None},
    {"market_positions": [], "cursor": False},
    {"market_positions": [], "cursor": 0},
    {"market_positions": [], "cursor": []},
    {"market_positions": [], "cursor": {}},
    {"market_positions": [], "cursor": " "},
    {"market_positions": [], "cursor": "a\nb"},
    {"market_positions": [], "cursor": "a\x7fb"},
    {"market_positions": [], "cursor": "a" * 4097},
])
async def test_ambiguous_authenticated_cursor_cannot_prove_empty_inventory(payload, demo_pem):
    transport = Mock(return_value=httpx.Response(200, json=payload))
    venue = Kalshi("fixture-key", demo_pem, _transport=httpx.MockTransport(transport))
    with pytest.raises(VenueError, match="^PAGINATION_INCOMPLETE$"):
        await venue.positions()
    transport.assert_called_once()


@pytest.mark.parametrize("payload", [
    {"cursor": ""}, {"market_positions": None, "cursor": ""},
    {"market_positions": {}, "cursor": ""}, {"market_positions": [position(), None], "cursor": ""},
])
async def test_invalid_page_never_yields_partial_rows(payload, demo_pem):
    transport = Mock(return_value=httpx.Response(200, json=payload))
    venue = Kalshi("fixture-key", demo_pem, _transport=httpx.MockTransport(transport))
    stream = venue._pages("/portfolio/positions", "market_positions", {"limit": 200}, auth=True)
    try:
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
    finally:
        await stream.aclose()
    transport.assert_called_once()


@pytest.mark.parametrize("case,expected", [("repeat", 2), ("cycle", 3), ("budget", 50)])
async def test_cursor_cycles_and_page_budget_fail_closed(case, expected, demo_pem):
    calls = []

    def respond(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        next_cursor = ("A" if case == "repeat" else
                       ("A" if len(calls) % 2 else "B") if case == "cycle" else
                       str(len(calls)))
        return httpx.Response(200, json={"market_positions": [], "cursor": next_cursor})

    venue = Kalshi("fixture-key", demo_pem, _transport=httpx.MockTransport(respond))
    with pytest.raises(VenueError, match="^PAGINATION_INCOMPLETE$"):
        await venue.positions()
    assert len(calls) == expected and calls[0] is None


async def test_explicit_empty_authenticated_inventory_is_valid(demo_pem):
    transport = Mock(return_value=httpx.Response(200, json={"market_positions": [], "cursor": ""}))
    venue = Kalshi("fixture-key", demo_pem, _transport=httpx.MockTransport(transport))
    assert await venue.positions() == []
    transport.assert_called_once()


@pytest.mark.parametrize("payload", [{"markets": []}, {"markets": [], "cursor": None}, {"markets": [], "cursor": ""}])
async def test_public_discovery_empty_page_legacy_compatibility(payload, demo_pem):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == PUBLIC
        assert request.url.path == ROOT + "/markets"
        assert "KALSHI-ACCESS-KEY" not in request.headers
        return httpx.Response(200, json=payload)

    venue = Kalshi("fixture-key", demo_pem, public=True, _transport=httpx.MockTransport(respond))
    assert await venue.markets() == []
    assert len(calls) == 1


# CD-25: authenticated read-only portfolio and reconciliation contracts.
def fixture_order(**overrides):
    return {"order_id": "order-1", "ticker": "FIXTURE", "client_order_id": "client-1",
            "outcome_side": "yes", "book_side": "bid", "fill_count_fp": "0",
            "remaining_count_fp": "2", "initial_count_fp": "2", "status": "resting",
            "maker_fees_dollars": "0", "taker_fees_dollars": "0",
            "yes_price_dollars": "0.42", "no_price_dollars": "0.58", **overrides}


def fixture_fill(**overrides):
    return {"order_id": "order-1", "ticker": "FIXTURE", "fill_id": "fill-1",
            "outcome_side": "yes", "count_fp": "1.25", "yes_price_dollars": "0.42",
            "no_price_dollars": "0.58", "is_taker": True, "fee_cost": "0.0012",
            "ts": 1700000000, **overrides}


def fixture_settlement(**overrides):
    return {"ticker": "FIXTURE", "event_ticker": "FIXTURE-EVENT", "market_result": "yes",
            "yes_count_fp": "2", "no_count_fp": "0", "revenue": 200,
            "yes_total_cost_dollars": "0.84", "no_total_cost_dollars": "0",
            "fee_cost": "0.01", "settled_time": "2026-11-20T00:00:00Z", "value": 100, **overrides}


def reader(demo_pem, replies):
    calls = []

    def respond(request):
        assert request.method == "GET" and request.url.host == DEMO
        assert request.headers["KALSHI-ACCESS-KEY"] == "fixture-key"
        path = request.url.path.removeprefix(ROOT)
        calls.append((path, dict(request.url.params)))
        payload = replies[path]
        return httpx.Response(200, json=payload(request) if callable(payload) else payload)

    return Kalshi("fixture-key", demo_pem, _transport=httpx.MockTransport(respond)), calls


@pytest.mark.parametrize("balance", [{"balance_dollars": "-0.01", "portfolio_value": 0},
                                     {"balance": 100, "portfolio_value": -1}])
async def test_negative_demo_balance_or_portfolio_value_rejected(demo_pem, balance):
    venue, calls = reader(demo_pem, {"/portfolio/balance": balance})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.verify_demo()
    assert len(calls) == 1


@pytest.mark.parametrize("rows,filter_ticker", [
    ([position(), position()], None),
    ([position("OTHER")], "FIXTURE"),
    ([position(fees_paid_dollars="-0.01")], None),
])
async def test_inconsistent_positions_rejected(demo_pem, rows, filter_ticker):
    venue, calls = reader(demo_pem, {"/portfolio/positions": {"market_positions": rows, "cursor": ""}})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.positions(filter_ticker)
    assert calls[0][1].get("ticker") == filter_ticker


async def test_filtered_position_preserves_signed_quantity(demo_pem):
    venue, calls = reader(demo_pem, {"/portfolio/positions": {"market_positions": [position()], "cursor": ""}})
    result = await venue.positions("FIXTURE")
    assert result[0]["quantity"] == Decimal("-1.25")
    assert calls[0][1]["ticker"] == "FIXTURE"


@pytest.mark.parametrize("rows,expected", [
    ([], None),
    ([fixture_order(client_order_id="unrelated")], None),
    ([fixture_order()], "order-1"),
])
async def test_find_order_only_returns_matching_identity(demo_pem, rows, expected):
    venue, calls = reader(demo_pem, {"/portfolio/orders": {"orders": rows, "cursor": ""}})
    found = await venue.find_demo_order("client-1", "FIXTURE")
    assert (found["broker_id"] if found else None) == expected
    assert calls[0][1]["ticker"] == "FIXTURE"


@pytest.mark.parametrize("rows,code", [
    ([fixture_order(), fixture_order(order_id="order-2")], "ORDER_CONFLICT"),
    ([fixture_order(), fixture_order()], "INVALID_RESPONSE"),
    ([fixture_order(ticker="OTHER")], "INVALID_RESPONSE"),
])
async def test_find_order_never_treats_conflict_as_absence(demo_pem, rows, code):
    venue, _ = reader(demo_pem, {"/portfolio/orders": {"orders": rows, "cursor": ""}})
    with pytest.raises(VenueError, match=f"^{code}$") as error:
        await venue.find_demo_order("client-1", "FIXTURE")
    assert error.value.outcome_unknown


@pytest.mark.parametrize("rows", [
    [fixture_order(), fixture_order()],
    [fixture_order(status="canceled", remaining_count_fp="0")],
    [fixture_order(remaining_count_fp="0")],
])
async def test_invalid_resting_inventory_rejected(demo_pem, rows):
    venue, _ = reader(demo_pem, {"/portfolio/orders": {"orders": rows, "cursor": ""}})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.open_orders()


@pytest.mark.parametrize("changes,code", [
    ({"outcome_side": "other"}, "INVALID_RESPONSE"),
    ({"book_side": "ask"}, "INVALID_RESPONSE"),
    ({"initial_count_fp": "0"}, "INVALID_RESPONSE"),
    ({"fill_count_fp": "3"}, "INVALID_RESPONSE"),
    ({"status": "other"}, "INVALID_RESPONSE"),
    ({"taker_fees_dollars": "-0.01"}, "INVALID_RESPONSE"),
    ({"yes_price_dollars": "0"}, "INVALID_RESPONSE"),
    ({"yes_price_dollars": "1"}, "INVALID_RESPONSE"),
    ({"no_price_dollars": "0.57"}, "INVALID_RESPONSE"),
    ({"client_order_id": []}, "INVALID_IDENTIFIER"),
    ({"client_order_id": "bad space"}, "INVALID_IDENTIFIER"),
    ({"created_time": "2026-11-20T00:00:00"}, "INVALID_RESPONSE"),
])
async def test_invalid_normalized_order_rejected(demo_pem, changes, code):
    venue, _ = reader(demo_pem, {"/portfolio/orders": {"orders": [fixture_order(**changes)], "cursor": ""}})
    with pytest.raises(VenueError, match=f"^{code}$"):
        await venue.open_orders()


@pytest.mark.parametrize("changes", [{"ticker": "OTHER"}, {"order_id": "other"}])
async def test_reconcile_wrong_order_identity_blocks_further_reads(demo_pem, changes):
    venue, calls = reader(demo_pem, {"/portfolio/orders/order-1": {"order": fixture_order(**changes)}})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.reconcile_demo("order-1", "FIXTURE")
    assert len(calls) == 1


@pytest.mark.parametrize("fills", [
    [fixture_fill(order_id="other")], [fixture_fill(ticker="OTHER")],
    [fixture_fill(), fixture_fill()], [fixture_fill(outcome_side="other")],
    [fixture_fill(no_price_dollars="0.57")], [fixture_fill(count_fp="0")],
    [fixture_fill(fee_cost="-0.01")], [fixture_fill(is_taker=1)],
    [fixture_fill(ts=0)], [fixture_fill(ts_ms=-1)],
])
async def test_invalid_reconciliation_fills_rejected_before_settlements(demo_pem, fills):
    venue, calls = reader(demo_pem, {
        "/portfolio/orders/order-1": {"order": fixture_order()},
        "/portfolio/fills": {"fills": fills, "cursor": ""},
    })
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.reconcile_demo("order-1", "FIXTURE")
    assert [path for path, _ in calls] == ["/portfolio/orders/order-1", "/portfolio/fills"]


@pytest.mark.parametrize("changes", [
    {"created_time": "2026-11-20T00:00:00Z"}, {"ts_ms": 1700000000123},
])
async def test_reconcile_fill_alternate_timestamp_formats(demo_pem, changes):
    venue, _ = reader(demo_pem, {
        "/portfolio/orders/order-1": {"order": fixture_order()},
        "/portfolio/fills": {"fills": [fixture_fill(**changes)], "cursor": ""},
        "/portfolio/settlements": {"settlements": [], "cursor": ""},
    })
    result = await venue.reconcile_demo("order-1", "FIXTURE")
    assert result["fills"][0]["quantity"] == Decimal("1.25")
    assert result["fills"][0]["observed_at"] > 0 and result["settlements"] == []


@pytest.mark.parametrize("rows", [
    [fixture_settlement(ticker="OTHER")],
    [fixture_settlement(), fixture_settlement()],
    [fixture_settlement(market_result="unknown")],
    [fixture_settlement(market_result="yes", value=0)],
    [fixture_settlement(market_result="scalar", value=None)],
    [fixture_settlement(revenue=-1)],
    [fixture_settlement(yes_total_cost_dollars="-0.01")],
    [fixture_settlement(no_total_cost_dollars="-0.01")],
    [fixture_settlement(fee_cost="-0.01")],
    [fixture_settlement(settled_time="1970-01-01T00:00:00Z")],
])
async def test_invalid_settlement_rejected(demo_pem, rows):
    venue, _ = reader(demo_pem, {"/portfolio/settlements": {"settlements": rows, "cursor": ""}})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.settlements("FIXTURE")


@pytest.mark.parametrize("result,value,payout", [
    ("yes", None, Decimal(1)), ("no", None, Decimal(0)),
    ("scalar", 35, Decimal("0.35")),
])
async def test_settlement_normalizes_known_payout_without_synthesizing_scalar(demo_pem, result, value, payout):
    venue, _ = reader(demo_pem, {"/portfolio/settlements": {
        "settlements": [fixture_settlement(market_result=result, value=value)], "cursor": "",
    }})
    rows = await venue.settlements("FIXTURE")
    assert rows[0]["yes_payout"] == payout
    assert rows[0]["cost"] == Decimal("0.84")
