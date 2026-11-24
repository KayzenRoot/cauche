"""CD-21: isolated Deriv quote, account balance and acknowledgement contracts."""

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.integrations import Deriv, VenueError
from app.integrations import common, deriv

NOW = 1800000000
SYMBOL = "frxEURUSD"
ACCOUNT = "DOT90000000"
ROOT = "/trading/v1/options/accounts"
OTP = "wss://api.derivws.com/trading/v1/options/ws/demo?otp=fixture-only"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(deriv, "pace", AsyncMock())
    monkeypatch.setattr(deriv, "time", Mock(time=lambda: NOW))


class FixtureSocket:
    def __init__(self, values):
        self.values = values
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        request = self.sent[-1]
        command = next(iter(request))
        response = {"req_id": request["req_id"], "msg_type": command, command: self.values[command]}
        if command == "proposal" and request.get("subscribe") == 1:
            response["subscription"] = {"id": self.values[command]["id"]}
        return json.dumps(response)


def fixture_adapter(*, balance=None, contract=None, proposal=None, purchase=None):
    contract_row = {
        "underlying_symbol": SYMBOL, "contract_type": "CALL", "contract_category": "callput",
        "barriers": 1, "min_contract_duration": "3m", "max_contract_duration": "2h",
        **(contract or {}),
    }
    values = {
        "balance": {"loginid": ACCOUNT, "currency": "USD", "balance": "100", **(balance or {})},
        "contracts_for": {"available": [contract_row]},
        "proposal": {"id": "proposal-fixture", "ask_price": "1", "payout": "1.85",
                     "spot": "1.25", "spot_time": NOW, **(proposal or {})},
        "buy": {"contract_id": 182, "transaction_id": 183, "buy_price": "1",
                "payout": "1.85", "balance_after": "99", "purchase_time": NOW, **(purchase or {})},
    }
    socket = FixtureSocket(values)
    routes, opened, closed = [], [], []

    def respond(request):
        routes.append((request.method, request.url.path))
        assert request.url.host == "api.derivws.com"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.method == "GET":
            assert request.url.path == ROOT
            return httpx.Response(200, json={"data": [
                {"account_id": ACCOUNT, "currency": "USD", "status": "active", "account_type": "demo"}
            ]})
        assert request.method == "POST" and request.url.path == f"{ROOT}/{ACCOUNT}/otp"
        return httpx.Response(200, json={"data": {"url": OTP}})

    @asynccontextmanager
    async def connect(url, **options):
        assert url in {OTP, "wss://api.derivws.com/trading/v1/options/ws/public"}
        assert options["proxy"] is None and options["additional_headers"] is None
        opened.append(url)
        try:
            yield socket
        finally:
            closed.append(url)

    venue = Deriv("fixture-app", "fixture-token", ACCOUNT, _transport=httpx.MockTransport(respond),
                  _connect=connect)
    return venue, socket, routes, opened, closed


def commands(socket):
    return [next(iter(request)) for request in socket.sent]


@pytest.mark.parametrize("side,stake,duration,unit,error", [
    ("OTHER", "1", 5, "m", "INVALID_CONTRACT"),
    ("CALL", "1", 0, "m", "INVALID_CONTRACT"),
    ("CALL", "1", 10081, "m", "INVALID_CONTRACT"),
    ("CALL", "1", True, "m", "INVALID_CONTRACT"),
    ("CALL", "1", 5.5, "m", "INVALID_CONTRACT"),
    ("CALL", "1", 5, "invalid", "INVALID_CONTRACT"),
    ("CALL", "0", 5, "m", "INVALID_STAKE"),
    ("CALL", "10000.01", 5, "m", "INVALID_STAKE"),
    ("CALL", "1.001", 5, "m", "INVALID_STAKE"),
])
async def test_invalid_quote_parameters_never_send_rpc(side, stake, duration, unit, error):
    venue, socket, routes, opened, closed = fixture_adapter()
    with pytest.raises(VenueError, match=f"^{error}$"):
        await venue.quote(SYMBOL, side, Decimal(stake), duration=duration, duration_unit=unit)
    assert not socket.sent and not routes
    assert opened == closed


@pytest.mark.parametrize("contract", [
    {"contract_type": "PUT"}, {"contract_category": "other"},
    {"barriers": 2}, {"barriers": True},
    {"min_contract_duration": "invalid"}, {"max_contract_duration": "4m"},
])
async def test_unavailable_contract_never_requests_proposal(contract):
    venue, socket, routes, opened, closed = fixture_adapter(contract=contract)
    with pytest.raises(VenueError, match="^CONTRACT_UNAVAILABLE$"):
        await venue.quote(SYMBOL, "CALL", Decimal(1))
    assert commands(socket) == ["contracts_for"]
    assert not routes and opened == closed


@pytest.mark.parametrize("proposal,error", [
    ({"ask_price": "0"}, "QUOTE_RISK_BLOCKED"),
    ({"ask_price": "1.01"}, "QUOTE_RISK_BLOCKED"),
    ({"payout": "1"}, "QUOTE_RISK_BLOCKED"),
    ({"spot": "0"}, "QUOTE_RISK_BLOCKED"),
    ({"spot_time": NOW - 11}, "STALE_QUOTE"),
    ({"spot_time": NOW + 1}, "STALE_QUOTE"),
])
async def test_invalid_quote_never_reaches_simulated_buy(proposal, error):
    venue, socket, _, opened, closed = fixture_adapter(proposal=proposal)
    with pytest.raises(VenueError, match=f"^{error}$") as caught:
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture")
    assert not caught.value.outcome_unknown
    assert commands(socket) == ["balance", "contracts_for", "proposal"]
    assert opened == closed


@pytest.mark.parametrize("balance,error", [
    ({"loginid": "OTHER"}, "DEMO_ACCOUNT_MISMATCH"),
    ({"currency": "EUR"}, "DEMO_ACCOUNT_MISMATCH"),
    ({"balance": "-0.01"}, "INVALID_RESPONSE"),
    ({"balance": "NaN"}, "INVALID_RESPONSE"),
])
async def test_invalid_demo_balance_blocks_before_quote(balance, error):
    venue, socket, _, opened, closed = fixture_adapter(balance=balance)
    with pytest.raises(VenueError, match=f"^{error}$"):
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture")
    assert commands(socket) == ["balance"]
    assert opened == closed


async def test_verify_demo_exact_balance_and_identity():
    venue, socket, routes, opened, closed = fixture_adapter(balance={"balance": "100.00000001"})
    result = await venue.verify_demo()
    assert result["account_id"] == ACCOUNT and result["currency"] == "USD"
    assert result["balance"] == Decimal("100.00000001")
    assert result["source"] == "DERIV_DEMO" and result["environment"] == "BROKER_DEMO"
    assert result["observed_at"] == NOW
    assert commands(socket) == ["balance"]
    assert routes == [("GET", ROOT), ("POST", f"{ROOT}/{ACCOUNT}/otp")]
    assert opened == closed == [OTP]


@pytest.mark.parametrize("limits", [
    {"expected_spot": Decimal("1.25")},
    {"max_spot_change": Decimal("0.01")},
    {"expected_spot": Decimal(0), "max_spot_change": Decimal("0.01")},
    {"expected_spot": Decimal("1.25"), "max_spot_change": Decimal("-0.01")},
])
async def test_incomplete_or_invalid_spot_limit_rejected_before_io(limits):
    venue, socket, routes, opened, _ = fixture_adapter()
    with pytest.raises(VenueError, match="^INVALID_SPOT_LIMIT$"):
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture", **limits)
    assert not socket.sent and not routes and not opened


async def test_invalid_payout_floor_rejected_before_io():
    venue, socket, routes, opened, _ = fixture_adapter()
    with pytest.raises(VenueError, match="^INVALID_PAYOUT_FLOOR$"):
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture",
                                min_payout_ratio=Decimal("0.99"))
    assert not socket.sent and not routes and not opened


async def test_spot_movement_blocks_simulated_buy():
    venue, socket, _, opened, closed = fixture_adapter()
    with pytest.raises(VenueError, match="^PRICE_MOVED$"):
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture",
                                expected_spot=Decimal("1.20"), max_spot_change=Decimal("0.01"))
    assert commands(socket) == ["balance", "contracts_for", "proposal"]
    assert opened == closed


async def test_insufficient_balance_blocks_simulated_buy():
    venue, socket, _, opened, closed = fixture_adapter(balance={"balance": "0.99"})
    with pytest.raises(VenueError, match="^QUOTE_RISK_BLOCKED$"):
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture")
    assert commands(socket) == ["balance", "contracts_for", "proposal"]
    assert opened == closed


@pytest.mark.parametrize("purchase,error", [
    ({"buy_price": "1.01"}, "BROKER_QUOTE_DIVERGENCE"),
    ({"buy_price": "0"}, "BROKER_QUOTE_DIVERGENCE"),
    ({"payout": "1.59"}, "BROKER_QUOTE_DIVERGENCE"),
    ({"contract_id": 0}, "INVALID_RESPONSE"),
    ({"contract_id": -1}, "INVALID_RESPONSE"),
    ({"contract_id": 10**19}, "INVALID_RESPONSE"),
    ({"contract_id": True}, "INVALID_RESPONSE"),
    ({"transaction_id": 0}, "INVALID_RESPONSE"),
    ({"transaction_id": None}, "INVALID_RESPONSE"),
    ({"balance_after": "-0.01"}, "INVALID_RESPONSE"),
    ({"balance_after": "Infinity"}, "INVALID_RESPONSE"),
    ({"purchase_time": NOW - 1}, "INVALID_RESPONSE"),
    ({"purchase_time": NOW + 1}, "INVALID_RESPONSE"),
    ({"purchase_time": True}, "INVALID_RESPONSE"),
    ({"account_id": "OTHER"}, "INVALID_RESPONSE"),
    ({"currency": "EUR"}, "INVALID_RESPONSE"),
])
async def test_invalid_purchase_acknowledgement_is_ambiguous_not_retried(purchase, error):
    venue, socket, routes, opened, closed = fixture_adapter(purchase=purchase)
    with pytest.raises(VenueError, match=f"^{error}$") as caught:
        await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture")
    assert caught.value.outcome_unknown and not caught.value.retryable
    assert commands(socket) == ["balance", "contracts_for", "proposal", "buy"]
    assert routes == [("GET", ROOT), ("POST", f"{ROOT}/{ACCOUNT}/otp")]
    assert opened == closed == [OTP]


async def test_valid_simulated_acknowledgement_preserves_exact_values_and_spot_limit():
    venue, socket, _, opened, closed = fixture_adapter(purchase={
        "account_id": ACCOUNT, "currency": "USD", "balance_after": "99.00000001",
    })
    result = await venue.submit_demo(SYMBOL, "CALL", Decimal(1), client_order_id="fixture",
                                     expected_spot=Decimal("1.24"), max_spot_change=Decimal("0.01"))
    assert result["broker_id"] == "182" and result["transaction_id"] == "183"
    assert result["client_order_id"] == "fixture" and result["status"] == "OPEN"
    assert result["price"] == Decimal(1) and result["payout"] == Decimal("1.85")
    assert result["balance"] == Decimal("99.00000001")
    assert result["observed_at"] == NOW and result["source"] == "DERIV_DEMO"
    assert commands(socket) == ["balance", "contracts_for", "proposal", "buy"]
    assert opened == closed == [OTP]
