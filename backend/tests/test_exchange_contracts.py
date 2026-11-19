"""Mocked contracts. No official account credentials or outbound traffic are required."""

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from app.integrations import Deriv, Kalshi, VenueError
from app.integrations import common, deriv
from app.integrations.kalshi import fee_estimate

DERIV_ACCOUNTS = "/trading/v1/options/accounts"
KALSHI_ROOT = "/trade-api/v2"
TEST_SYMBOL = "frxEURUSD"
TEST_MARKET = "TEST-2026NOV-B53.5"
TEST_ORDER = "order-123"
DEMO_ACCOUNT = "DOT90000000"
TEST_CLIENT = "intent-a1b2"


@pytest.fixture(autouse=True)
def skip_pacing(monkeypatch):
    async def no_wait(*_args):
        pass

    monkeypatch.setattr(common, "pace", no_wait)
    monkeypatch.setattr(deriv, "pace", no_wait)


@pytest.fixture(scope="module")
def rsa_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _pem(private), private.public_key()


def _pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def run(awaitable):
    return asyncio.run(awaitable)


class FakeSocket:
    def __init__(self, responder):
        self.responder = responder
        self.sent = []
        self.interleaved = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if "buy" in self.sent[-1] and not self.interleaved:
            self.interleaved = True
            proposal = self.sent[-2]
            assert proposal["subscribe"] == 1
            return json.dumps(
                {
                    "msg_type": "proposal",
                    "req_id": proposal["req_id"],
                    "subscription": {"id": "proposal-123"},
                }
            )
        return json.dumps(self.responder(self.sent[-1]))


class BodySocket(FakeSocket):
    def __init__(self, messages):
        super().__init__(None)
        self.messages = iter(messages)

    async def recv(self):
        return json.dumps(next(self.messages))


def _socket_factory(responder, urls):
    @asynccontextmanager
    async def connection(url, **kwargs):
        urls.append((url, kwargs))
        yield FakeSocket(responder)

    return connection


def _deriv_account(account=DEMO_ACCOUNT, account_type="demo"):
    return {
        "account_id": account,
        "currency": "USD",
        "status": "active",
        "balance": 100,
        "account_type": account_type,
        "group": "row",
    }


def _deriv_rest(calls, *, account_type="demo", otp_url=None, currency="USD"):
    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.url.host == "api.derivws.com"
        assert request.headers["Deriv-App-ID"] == "app-1"
        assert request.headers["Authorization"] == "Bearer fixture-deriv-credential"
        if request.url.path == DERIV_ACCOUNTS:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            **_deriv_account(account_type=account_type),
                            "currency": currency,
                        }
                    ]
                },
            )
        assert request.url.path == f"{DERIV_ACCOUNTS}/{DEMO_ACCOUNT}/otp"
        return httpx.Response(
            200,
            json={
                "data": {
                    "url": otp_url
                    or "wss://api.derivws.com/trading/v1/options/ws/demo?otp=test-fixture"
                },
            },
        )

    return httpx.MockTransport(respond)


def _deriv_responder(calls, *, loss=False, payout="1.85"):
    def responder(request):
        command = next(iter(request))
        calls.append(command)
        if command == "balance":
            data = {"balance": 100, "currency": "USD", "loginid": DEMO_ACCOUNT}
        elif command == "contracts_for":
            data = {
                "available": [
                    {
                        "underlying_symbol": TEST_SYMBOL,
                        "contract_type": "CALL",
                        "contract_category": "callput",
                        "barriers": 1,
                        "min_contract_duration": "3m",
                        "max_contract_duration": "2h",
                    }
                ]
            }
        elif command == "proposal":
            assert request["underlying_symbol"] == TEST_SYMBOL
            assert "symbol" not in request and request["amount"] == 1
            assert "barrier" not in request
            assert request["duration"] == 5 and request["duration_unit"] == "m"
            data = {
                "id": "proposal-123",
                "ask_price": "1",
                "payout": payout,
                "spot": "1.05010",
                "spot_time": int(time.time()),
            }
        elif command == "buy":
            assert request["buy"] == "proposal-123" and request["price"] == 1
            assert request["passthrough"]["client_order_id"] == TEST_CLIENT
            if loss:
                raise TimeoutError("secret-at-upstream")
            data = {
                "contract_id": 182,
                "transaction_id": 183,
                "buy_price": "1",
                "payout": "1.85",
                "balance_after": "99",
                "purchase_time": int(time.time()),
            }
        elif command == "proposal_open_contract":
            assert request["contract_id"] == 182
            data = {
                "contract_id": 182,
                "currency": "USD",
                "is_sold": 1,
                "buy_price": "1",
                "sell_price": "1.85",
                "profit": "0.85",
                "sell_time": int(time.time()),
            }
        elif command == "active_symbols":
            data = [
                {
                    "underlying_symbol": TEST_SYMBOL,
                    "underlying_symbol_name": "EUR/USD",
                    "market": "forex",
                    "exchange_is_open": 1,
                    "is_trading_suspended": 0,
                }
            ]
        elif command == "ticks_history":
            timestamp = int(time.time()) // 300 * 300
            old = {
                "epoch": timestamp - 300,
                "open": 1.05,
                "low": 1.04,
                "high": 1.08,
                "close": 1.07,
            }
            future = {**old, "epoch": timestamp}
            command, data = "candles", [future, old]
        else:
            pytest.fail(f"Unexpected operation: {command}")
        response = {"req_id": request["req_id"], "msg_type": command, command: data}
        if command == "proposal" and request.get("subscribe") == 1:
            response["subscription"] = {"id": data["id"]}
        return response

    return responder


def test_deriv_order_discovery_quote_buy_and_settlement():
    routes, sockets, calls = [], [], []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        DEMO_ACCOUNT,
        _transport=_deriv_rest(routes),
        _connect=_socket_factory(_deriv_responder(calls), sockets),
    )
    order = run(
        venue.submit_demo(TEST_SYMBOL, "CALL", Decimal(1), client_order_id=TEST_CLIENT)
    )
    assert (
        order["price"] == 1
        and order["status"] == "OPEN"
        and order["broker_id"] == "182"
    )
    assert routes == [
        ("GET", DERIV_ACCOUNTS),
        ("POST", f"{DERIV_ACCOUNTS}/{DEMO_ACCOUNT}/otp"),
    ]
    assert calls == ["balance", "contracts_for", "proposal", "buy"]
    assert all("/ws/demo?" in socket[0] for socket in sockets)
    reconciled = run(venue.reconcile_demo("182"))
    assert reconciled["status"] == "SETTLED" and reconciled["profit"] == Decimal("0.85")


@pytest.mark.parametrize(
    "target",
    [
        "wss://api.derivws.com/trading/v1/options/ws/real?otp=some-secret",
        "wss://api.derivws.com.attacker.test/trading/v1/options/ws/demo?otp=some-secret",
        "ws://api.derivws.com/trading/v1/options/ws/demo?otp=some-secret",
        "wss://api.derivws.com/trading/v1/options/ws/demo?otp=some-secret&otp=twice",
        "wss://api.derivws.com/trading/v1/options/ws/demo?otp=%0d%0a",
        "wss://api.derivws.com:443/trading/v1/options/ws/demo?otp=some-secret",
    ],
)
def test_deriv_rejects_unsafe_otp_without_opening_socket(target):
    routes, sockets = [], []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        _transport=_deriv_rest(routes, otp_url=target),
        _connect=_socket_factory(_deriv_responder([]), sockets),
    )
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        run(
            venue.submit_demo(
                TEST_SYMBOL, "CALL", Decimal(1), client_order_id=TEST_CLIENT
            )
        )
    assert not sockets


def test_deriv_rejects_real_account_before_otp_and_ws():
    routes, sockets = [], []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        DEMO_ACCOUNT,
        _transport=_deriv_rest(routes, account_type="real"),
        _connect=_socket_factory(_deriv_responder([]), sockets),
    )
    with pytest.raises(VenueError, match="^DEMO_ACCOUNT_REQUIRED$"):
        run(venue.verify_demo())
    assert routes == [("GET", DERIV_ACCOUNTS)] and not sockets


def test_deriv_rejects_unexpected_currency_before_any_socket_command():
    calls = []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        DEMO_ACCOUNT,
        _transport=_deriv_rest([], currency="EUR"),
        _connect=_socket_factory(_deriv_responder(calls), []),
    )
    with pytest.raises(VenueError, match="^UNSUPPORTED_CURRENCY$"):
        run(
            venue.submit_demo(
                TEST_SYMBOL, "CALL", Decimal(1), client_order_id=TEST_CLIENT
            )
        )
    assert not calls


def test_deriv_never_retries_ambiguous_buy():
    calls = []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        DEMO_ACCOUNT,
        _transport=_deriv_rest([]),
        _connect=_socket_factory(_deriv_responder(calls, loss=True), []),
    )
    with pytest.raises(VenueError, match="^DISCONNECTED$") as failure:
        run(
            venue.submit_demo(
                TEST_SYMBOL, "CALL", Decimal(1), client_order_id=TEST_CLIENT
            )
        )
    assert (
        failure.value.outcome_unknown
        and not failure.value.retryable
        and calls.count("buy") == 1
    )


def test_deriv_filters_incomplete_bars_and_supports_official_public_quote():
    sockets, calls = [], []
    venue = Deriv(_connect=_socket_factory(_deriv_responder(calls), sockets))
    candles = run(venue.candles(TEST_SYMBOL))
    quote = run(venue.quote(TEST_SYMBOL, "CALL", Decimal(1)))
    assert len(candles) == 1 and candles[0]["close"] == Decimal("1.07")
    assert quote["stake"] == Decimal(1) and quote["payout"] == Decimal("1.85")
    assert all(url.endswith("/ws/public") for url, _ in sockets)
    assert "buy" not in calls


def test_deriv_blocks_low_payout_after_demo_verification():
    calls = []
    venue = Deriv(
        "app-1",
        "fixture-deriv-credential",
        _transport=_deriv_rest([]),
        _connect=_socket_factory(_deriv_responder(calls, payout="1.1"), []),
    )
    with pytest.raises(VenueError, match="^QUOTE_RISK_BLOCKED$"):
        run(
            venue.submit_demo(
                TEST_SYMBOL, "CALL", Decimal(1), client_order_id=TEST_CLIENT
            )
        )
    assert "proposal" in calls and "buy" not in calls


def _market():
    return {
        "ticker": TEST_MARKET,
        "event_ticker": "TEST-EVENT",
        "market_type": "binary",
        "status": "active",
        "result": "",
        "close_time": "2099-12-31T00:00:00Z",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def _verify_signature(request, public_key):
    ts = request.headers["KALSHI-ACCESS-TIMESTAMP"]
    assert ts.isdecimal() and len(ts) == 13
    data = f"{ts}{request.method}{request.url.path}".encode()
    signature = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"])
    if isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(
            signature,
            data,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )
    else:
        public_key.verify(signature, data)


def _kalshi_order_payload(request):
    data = json.loads(request.content)
    assert data["ticker"] == TEST_MARKET and data["client_order_id"] == TEST_CLIENT
    assert (
        data["price"] == "0.6200" and data["count"] == "2.50" and data["side"] == "ask"
    )
    assert data["time_in_force"] == "immediate_or_cancel"
    assert data["self_trade_prevention_type"] == "taker_at_cross"
    return {
        "order_id": TEST_ORDER,
        "client_order_id": TEST_CLIENT,
        "fill_count": "1.25",
        "remaining_count": "0.00",
        "average_fill_price": "0.6300",
        "average_fee_paid": "0.0012",
        "ts_ms": 1700000000123,
    }


def test_kalshi_v2_no_limit_partial_fill_exact_amounts_and_cancel_signature(rsa_pair):
    pem, public = rsa_pair
    requests = []

    def responder(request):
        requests.append(request.method)
        assert request.url.host == "external-api.demo.kalshi.co"
        if request.url.path == f"{KALSHI_ROOT}/markets/{TEST_MARKET}":
            assert "KALSHI-ACCESS-KEY" not in request.headers
            return httpx.Response(200, json={"market": _market()})
        _verify_signature(request, public)
        if request.method == "POST":
            assert request.url.path == f"{KALSHI_ROOT}/portfolio/events/orders"
            return httpx.Response(201, json=_kalshi_order_payload(request))
        assert request.method == "DELETE"
        assert request.url.path == f"{KALSHI_ROOT}/portfolio/events/orders/{TEST_ORDER}"
        assert request.url.params["market_ticker"] == TEST_MARKET
        return httpx.Response(
            200,
            json={"order_id": TEST_ORDER, "reduced_by": "0.00", "ts_ms": 1700000002123},
        )

    venue = Kalshi("fixture-key", pem, _transport=httpx.MockTransport(responder))
    order = run(
        venue.submit_demo(
            TEST_MARKET,
            "NO",
            Decimal("0.38"),
            Decimal("2.50"),
            client_order_id=TEST_CLIENT,
        )
    )
    assert order["status"] == "PARTIAL" and order["remaining_quantity"] == 0
    assert order["filled_quantity"] == Decimal("1.25") and order[
        "average_fill_price"
    ] == Decimal("0.37")
    assert order["fee"] == Decimal("0.00150")
    assert run(venue.cancel_demo(TEST_ORDER, TEST_MARKET))["reduced_quantity"] == 0
    assert requests == ["GET", "POST", "DELETE"]


def test_kalshi_rejects_off_grid_without_order_write(rsa_pair):
    pem, _ = rsa_pair

    def responder(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"market": _market()})

    venue = Kalshi("fixture-key", pem, _transport=httpx.MockTransport(responder))
    with pytest.raises(VenueError, match="^PRICE_GRID_MISMATCH$"):
        run(
            venue.submit_demo(
                TEST_MARKET,
                "YES",
                Decimal("0.381"),
                Decimal(1),
                client_order_id=TEST_CLIENT,
            )
        )


def test_kalshi_public_data_never_sends_credentials_or_mutates(rsa_pair):
    pem, _ = rsa_pair

    def responder(request):
        assert request.url.host == "external-api.kalshi.com"
        assert request.method == "GET" and "KALSHI-ACCESS-KEY" not in request.headers
        return httpx.Response(200, json={"market": _market()})

    venue = Kalshi(
        "fixture-key", pem, public=True, _transport=httpx.MockTransport(responder)
    )
    assert (
        run(venue.market(TEST_MARKET, include_fees=False))["source"] == "KALSHI_PUBLIC"
    )
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        run(
            venue.submit_demo(
                TEST_MARKET,
                "YES",
                Decimal("0.38"),
                Decimal(1),
                client_order_id=TEST_CLIENT,
            )
        )
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        run(venue.verify_demo())
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        run(venue.cancel_demo(TEST_ORDER, TEST_MARKET))


@pytest.mark.parametrize(
    "failure", ["timeout", "malformed", "redirect", "status", "conflict"]
)
def test_kalshi_never_retries_or_redirects_ambiguous_order(rsa_pair, failure):
    pem, _ = rsa_pair
    calls = []

    def responder(request):
        assert request.url.host == "external-api.demo.kalshi.co"
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"market": _market()})
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream-secret")
        if failure == "malformed":
            return httpx.Response(201, json={"fill_count": "0"})
        if failure == "redirect":
            return httpx.Response(
                307, headers={"Location": "https://external-api.kalshi.com/write"}
            )
        return httpx.Response(503 if failure == "status" else 409)

    venue = Kalshi("fixture-key", pem, _transport=httpx.MockTransport(responder))
    with pytest.raises(VenueError) as error:
        run(
            venue.submit_demo(
                TEST_MARKET,
                "NO",
                Decimal("0.38"),
                Decimal("2.50"),
                client_order_id=TEST_CLIENT,
            )
        )
    assert error.value.outcome_unknown and not error.value.retryable
    assert "secret" not in str(error.value) and calls == ["GET", "POST"]


def test_kalshi_ed25519_and_independent_balance_bucket():
    signer = ed25519.Ed25519PrivateKey.generate()

    def responder(request):
        _verify_signature(request, signer.public_key())
        assert request.url.path == f"{KALSHI_ROOT}/portfolio/balance"
        return httpx.Response(
            200,
            json={"balance": 1353, "balance_dollars": "13.5301", "portfolio_value": 38},
        )

    venue = Kalshi(
        "fixture-key", _pem(signer), _transport=httpx.MockTransport(responder)
    )
    assert run(venue.verify_demo())["balance"] == Decimal("13.5301")


def test_kalshi_retry_reads_on_429_and_compute_complement_depth_and_fees():
    calls = []

    def responder(request):
        path = request.url.path.removeprefix(KALSHI_ROOT)
        calls.append(path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        replies = {
            f"/markets/{TEST_MARKET}": {"market": _market()},
            f"/markets/{TEST_MARKET}/orderbook": {
                "orderbook_fp": {
                    "yes_dollars": [["0.42", "13.25"]],
                    "no_dollars": [["0.56", "17.50"]],
                }
            },
            "/events/TEST-EVENT": {
                "event": {
                    "event_ticker": "TEST-EVENT",
                    "series_ticker": "TEST-SERIES",
                    "fee_type_override": "quadratic_with_maker_fees",
                    "fee_multiplier_override": 0.5,
                }
            },
            "/series/TEST-SERIES": {
                "series": {
                    "ticker": "TEST-SERIES",
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                    "category": "Test",
                }
            },
        }
        return httpx.Response(200, json=replies[path])

    venue = Kalshi(_transport=httpx.MockTransport(responder))
    market = run(venue.market(TEST_MARKET))
    book = run(venue.orderbook(TEST_MARKET, market=market))
    assert book["yes_ask"] == Decimal("0.44") and book["yes_ask_size"] == Decimal(
        "17.5"
    )
    assert book["no_ask"] == Decimal("0.58") and book["no_ask_size"] == Decimal("13.25")
    assert market["maker_fee_rate"] == Decimal(".00875") and market[
        "taker_fee_rate"
    ] == Decimal(".035")
    assert calls[:2] == [f"/markets/{TEST_MARKET}"] * 2
    assert fee_estimate(Decimal("0.55"), Decimal("0.1"), Decimal("0.07")) == Decimal(
        "0.0018"
    )


def test_kalshi_reconciliation_keeps_market_settlement_separate_from_order_fills(
    rsa_pair,
):
    pem, public = rsa_pair
    order = {
        "order_id": TEST_ORDER,
        "ticker": TEST_MARKET,
        "client_order_id": TEST_CLIENT,
        "outcome_side": "no",
        "book_side": "ask",
        "fill_count_fp": "1.25",
        "remaining_count_fp": "0.00",
        "initial_count_fp": "2.50",
        "status": "canceled",
        "maker_fees_dollars": "0.0000",
        "taker_fees_dollars": "0.0015",
        "yes_price_dollars": "0.62",
        "no_price_dollars": "0.38",
        "created_time": "2026-11-19T17:30:00Z",
    }
    fill = {
        "order_id": TEST_ORDER,
        "ticker": TEST_MARKET,
        "fill_id": "fill-1",
        "outcome_side": "no",
        "count_fp": "1.25",
        "yes_price_dollars": "0.63",
        "no_price_dollars": "0.37",
        "is_taker": True,
        "fee_cost": "0.0015",
        "ts": 1700000000,
    }
    settlement = {
        "ticker": TEST_MARKET,
        "event_ticker": "TEST-EVENT",
        "market_result": "no",
        "yes_count_fp": "0.00",
        "no_count_fp": "5.00",
        "revenue": 500,
        "yes_total_cost_dollars": "0",
        "no_total_cost_dollars": "1.85",
        "fee_cost": "0.006",
        "settled_time": "2026-11-20T00:00:00Z",
        "value": 0,
    }

    def responder(request):
        _verify_signature(request, public)
        route = request.url.path.removeprefix(KALSHI_ROOT)
        replies = {
            f"/portfolio/orders/{TEST_ORDER}": {"order": order},
            "/portfolio/fills": {"fills": [fill], "cursor": ""},
            "/portfolio/settlements": {"settlements": [settlement], "cursor": ""},
        }
        return httpx.Response(200, json=replies[route])

    venue = Kalshi("fixture-key", pem, _transport=httpx.MockTransport(responder))
    result = run(venue.reconcile_demo(TEST_ORDER, TEST_MARKET))
    assert result["order"]["status"] == "PARTIAL"
    assert result["fills"][0]["price"] == Decimal("0.37")
    assert result["settlements"][0]["no_quantity"] == 5
    assert result["settlements"][0]["revenue"] == 5
    assert result["settlements"][0]["yes_payout"] == 0


def test_kalshi_lists_paginated_resting_orders_with_demo_auth(rsa_pair):
    pem, public = rsa_pair
    page_cursors = []
    base_order = {
        "ticker": TEST_MARKET,
        "client_order_id": TEST_CLIENT,
        "outcome_side": "yes",
        "book_side": "bid",
        "fill_count_fp": "0",
        "remaining_count_fp": "1",
        "initial_count_fp": "1",
        "status": "resting",
        "maker_fees_dollars": "0",
        "taker_fees_dollars": "0",
        "yes_price_dollars": "0.62",
        "no_price_dollars": "0.38",
    }

    def responder(request):
        _verify_signature(request, public)
        assert (
            request.method == "GET"
            and request.url.host == "external-api.demo.kalshi.co"
        )
        assert request.url.path == f"{KALSHI_ROOT}/portfolio/orders"
        assert request.url.params["status"] == "resting"
        cursor = request.url.params.get("cursor")
        page_cursors.append(cursor)
        order = {**base_order, "order_id": f"order-{len(page_cursors)}"}
        return httpx.Response(
            200, json={"orders": [order], "cursor": "" if cursor else "next"}
        )

    venue = Kalshi("fixture-key", pem, _transport=httpx.MockTransport(responder))
    orders = run(venue.open_orders())
    assert [order["broker_id"] for order in orders] == ["order-1", "order-2"]
    assert all(order["remaining_quantity"] == 1 for order in orders)
    assert page_cursors == [None, "next"]


def test_kalshi_five_minute_history_aggregates_only_closed_observed_trades():
    def responder(request):
        assert request.url.path == f"{KALSHI_ROOT}/markets/candlesticks"
        assert request.url.params["period_interval"] == "1"
        end = int(request.url.params["end_ts"])
        opened = end - 300

        def market_bar(closed, prices, volume):
            return {
                "end_period_ts": closed,
                "volume_fp": volume,
                "price": dict(
                    zip(
                        (
                            "open_dollars",
                            "high_dollars",
                            "low_dollars",
                            "close_dollars",
                        ),
                        prices,
                        strict=True,
                    )
                ),
            }

        data = [
            market_bar(opened + 60, ("0.4", "0.45", "0.38", "0.43"), "2.50"),
            market_bar(opened + 180, ("0.43", "0.53", "0.39", "0.47"), "4.00"),
            market_bar(end + 60, ("0.47", "0.99", "0.47", "0.99"), "100.00"),
        ]
        return httpx.Response(
            200,
            json={"markets": [{"market_ticker": TEST_MARKET, "candlesticks": data}]},
        )

    candles = run(
        Kalshi(public=True, _transport=httpx.MockTransport(responder)).candles(
            TEST_MARKET, 300, 2
        )
    )
    assert len(candles) == 1
    assert {
        key: candles[0][key] for key in ("open", "high", "low", "close", "volume")
    } == {
        "open": Decimal("0.4"),
        "high": Decimal("0.53"),
        "low": Decimal("0.38"),
        "close": Decimal("0.47"),
        "volume": Decimal("6.50"),
    }


def test_kalshi_orderbook_stream_invalidates_on_sequence_gap(rsa_pair):
    pem, _ = rsa_pair
    socket = BodySocket(
        [
            {
                "type": "orderbook_snapshot",
                "seq": 1,
                "msg": {
                    "market_ticker": TEST_MARKET,
                    "yes_dollars_fp": [["0.42", "4.00"]],
                    "no_dollars_fp": [["0.55", "3.00"]],
                },
            },
            {
                "type": "orderbook_delta",
                "seq": 2,
                "msg": {
                    "market_ticker": TEST_MARKET,
                    "side": "no",
                    "price_dollars": "0.55",
                    "delta_fp": "1.00",
                },
            },
            {
                "type": "orderbook_delta",
                "seq": 4,
                "msg": {
                    "market_ticker": TEST_MARKET,
                    "side": "no",
                    "price_dollars": "0.55",
                    "delta_fp": "1.00",
                },
            },
        ]
    )

    @asynccontextmanager
    async def connection(url, **kwargs):
        assert url.startswith("wss://external-api-ws.demo.kalshi.co/")
        assert "KALSHI-ACCESS-SIGNATURE" in kwargs["additional_headers"]
        yield socket

    async def exercise():
        venue = Kalshi("fixture-key", pem, _connect=connection)
        stream = venue.stream_orderbook(TEST_MARKET)
        try:
            return [await anext(stream) for _ in range(3)]
        finally:
            await stream.aclose()

    first, second, gap = run(exercise())
    assert first["yes_ask_size"] == 3 and second["yes_ask_size"] == 4
    assert gap["status"] == "BOOK_SEQUENCE_GAP"
    assert socket.sent[0]["params"]["market_tickers"] == [TEST_MARKET]
