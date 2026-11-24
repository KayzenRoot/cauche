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


# CD-22: authenticated read-only inventory, statement and settlement contracts.
def position(**overrides):
    return {"contract_id": 182, "underlying_symbol": SYMBOL, "contract_type": "CALL",
            "buy_price": "1.00000001", "payout": "1.85", "purchase_time": NOW - 5, **overrides}


def transaction(**overrides):
    return {"transaction_id": 183, "contract_id": 182, "action_type": "buy",
            "amount": "-1.00000001", "transaction_time": NOW - 5, **overrides}


def contract_state(**overrides):
    return {"contract_id": 182, "currency": "USD", "is_sold": 1, "buy_price": "1",
            "sell_price": "1.85", "profit": "0.85", "sell_time": NOW - 1, **overrides}


def assert_read_only(socket, opened, closed, command):
    assert commands(socket) == ["balance", command]
    assert opened == closed == [OTP]


@pytest.mark.parametrize("rows", [[], [position()], [position(), position(contract_id=183, contract_type="OTHER")]])
async def test_portfolio_preserves_complete_inventory(rows):
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["portfolio"] = {"contracts": rows, "account_id": ACCOUNT, "currency": "USD"}
    result = await venue.portfolio()
    assert len(result) == len(rows)
    assert [r["broker_id"] for r in result] == [str(r["contract_id"]) for r in rows]
    assert all(r["price"] == Decimal("1.00000001") for r in result)
    assert [r["side"] for r in result] == [r["contract_type"] for r in rows]
    assert_read_only(socket, opened, closed, "portfolio")


@pytest.mark.parametrize("payload", [
    {}, {"contracts": None}, {"contracts": {}}, {"contracts": [None]},
    {"contracts": [position(), position()]}, {"contracts": [position(contract_id=0)]},
    {"contracts": [position(contract_id=True)]}, {"contracts": [position(contract_id=10**19)]},
    {"contracts": [position(buy_price="0")]}, {"contracts": [position(payout="-1")]},
    {"contracts": [position(purchase_time=0)]}, {"contracts": [position(purchase_time=NOW + 1)]},
    {"contracts": [position(account_id="OTHER")]}, {"contracts": [position(currency="EUR")]},
    {"contracts": [], "account_id": "OTHER"}, {"contracts": [], "currency": "EUR"},
])
async def test_invalid_inventory_never_looks_empty(payload):
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["portfolio"] = payload
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.portfolio()
    assert_read_only(socket, opened, closed, "portfolio")


@pytest.mark.parametrize("kwargs", [
    {"date_from": 0}, {"date_from": True}, {"date_from": NOW + 1},
    {"date_from": NOW, "limit": 0}, {"date_from": NOW, "limit": 101},
    {"date_from": NOW, "limit": True}, {"date_from": NOW, "offset": -1},
    {"date_from": NOW, "offset": 10001}, {"date_from": NOW, "offset": 0.5},
])
async def test_invalid_statement_range_never_opens_socket(kwargs):
    venue, socket, routes, opened, _ = fixture_adapter()
    with pytest.raises(VenueError, match="^INVALID_HISTORY_RANGE$"):
        await venue.statement(**kwargs)
    assert not socket.sent and not routes and not opened


async def test_statement_exact_values_and_explicit_pagination():
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["statement"] = {"transactions": [
        transaction(app_id=123, shortcode="fixture"),
        transaction(transaction_id=184, action_type="sell", amount="1.85"),
        {"transaction_id": 185, "action_type": "deposit", "transaction_time": NOW},
    ]}
    result = await venue.statement(date_from=NOW - 10, limit=3, offset=4)
    assert result == [
        {"transaction_id": "183", "broker_id": "182", "app_id": 123, "shortcode": "fixture",
         "action": "buy", "amount": Decimal("-1.00000001"), "observed_at": NOW - 5,
         "currency": "USD", "source": "DERIV_DEMO"},
        {"transaction_id": "184", "broker_id": "182", "app_id": None, "shortcode": None,
         "action": "sell", "amount": Decimal("1.85"), "observed_at": NOW - 5,
         "currency": "USD", "source": "DERIV_DEMO"},
    ]
    assert socket.sent[-1] == {"statement": 1, "description": 1, "date_from": NOW - 10,
                               "limit": 3, "offset": 4, "req_id": 2}
    assert_read_only(socket, opened, closed, "statement")


async def test_explicit_empty_statement_is_valid():
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["statement"] = {"transactions": []}
    assert await venue.statement(date_from=NOW) == []
    assert_read_only(socket, opened, closed, "statement")


@pytest.mark.parametrize("payload", [
    {}, {"transactions": None}, {"transactions": {}}, {"transactions": [None]},
    {"transactions": [transaction(), transaction(action_type="sell")]},
    {"transactions": [transaction(), transaction(transaction_id=184), transaction(transaction_id=185)]},
    {"transactions": [transaction(transaction_id=0)]}, {"transactions": [transaction(transaction_id=True)]},
    {"transactions": [transaction(contract_id=-1)]}, {"transactions": [transaction(contract_id=True)]},
    {"transactions": [transaction(transaction_time=NOW - 11)]},
    {"transactions": [transaction(transaction_time=NOW + 1)]},
    {"transactions": [transaction(action_type=None)]}, {"transactions": [transaction(amount="NaN")]},
    {"transactions": [transaction(account_id="OTHER")]}, {"transactions": [transaction(currency="EUR")]},
    {"transactions": [], "currency": "EUR"},
])
async def test_invalid_statement_fails_closed(payload):
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["statement"] = payload
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.statement(date_from=NOW - 10, limit=2)
    assert_read_only(socket, opened, closed, "statement")


@pytest.mark.parametrize("changes", [
    {"contract_id": 183}, {"contract_id": True}, {"contract_id": 0},
    {"account_id": "OTHER"}, {"account_id": []}, {"currency": "EUR"},
    {"is_sold": 2}, {"is_sold": True}, {"buy_price": "0"}, {"sell_price": "-1"},
    {"commission": "-0.01"}, {"sell_time": 0}, {"sell_time": NOW + 1},
    {"sell_time": True}, {"profit": "NaN"},
])
async def test_invalid_contract_state_does_not_return_settlement(changes):
    venue, socket, _, opened, closed = fixture_adapter()
    socket.values["proposal_open_contract"] = contract_state(**changes)
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.reconcile_demo("182")
    assert_read_only(socket, opened, closed, "proposal_open_contract")


@pytest.mark.parametrize("sold", [False, True])
async def test_contract_state_open_or_settled_exact_values(sold):
    venue, socket, _, opened, closed = fixture_adapter()
    row = contract_state(account_id=ACCOUNT, is_sold=int(sold), payout="1.85", commission="0.01")
    if sold:
        del row["sell_time"]
        row["date_settlement"] = NOW - 1
        row["profit"] = "0.84"
    socket.values["proposal_open_contract"] = row
    result = await venue.reconcile_demo("182")
    assert result == {"broker_id": "182", "status": "SETTLED" if sold else "OPEN",
                      "price": Decimal(1), "payout": Decimal("1.85"),
                      "profit": Decimal("0.84") if sold else None,
                      "fee": Decimal("0.01"), "settled_at": NOW - 1 if sold else None,
                      "observed_at": NOW, "currency": "USD", "source": "DERIV_DEMO"}
    assert_read_only(socket, opened, closed, "proposal_open_contract")


def test_wire_number_refuses_loss_of_decimal_precision():
    assert deriv._wire_number(Decimal("1.25")) == 1.25
    with pytest.raises(VenueError, match="^INVALID_PRECISION$"):
        deriv._wire_number(Decimal("999999999999999.1234567890123456"))
