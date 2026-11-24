"""CD-18: common adapter transport and strict JSON boundaries, no network."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from websockets.exceptions import InvalidStatus, WebSocketException

from app.integrations import common
from app.integrations.common import VenueError

URL = "https://fixture.invalid/contract"


def test_json_exact_decimal_and_nested_objects():
    result = common.parse_json(b'{"balance":100.12345678,"orders":[{"qty":1,"fee":0.0001}]}')
    assert result == {"balance": Decimal("100.12345678"), "orders": [{"qty": 1, "fee": Decimal("0.0001")}]}
    assert isinstance(result["balance"], Decimal)


@pytest.mark.parametrize("raw", [
    '{"balance":1,"balance":2}', '{"order":{"id":"one","id":"two"}}',
    '{"fills":[{"qty":1,"qty":2}]}', '{"a":1,"\\u0061":2}',
    '{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}', '{"n":[NaN]}',
    '{"n":1e999999999999999999999999999999999}', '{"token":"PRIVATE_FIXTURE"', b'\xff',
    '[]', 'null', '1', None, {},
])
def test_invalid_json_rejected_with_sanitized_error(raw):
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        common.parse_json(raw)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "1e15", "1e-17", "invalid", None])
def test_decimal_contract_rejects_invalid_values(value):
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        common.decimal(value)


def test_decimal_and_optional_exact_boundaries():
    assert common.decimal("999999999999999.9999999999999999") == Decimal("999999999999999.9999999999999999")
    assert common.decimal("0.0000000000000001") == Decimal("0.0000000000000001")
    assert common.optional_decimal(None) is None
    assert common.optional_decimal("0") == Decimal(0)


@pytest.mark.parametrize("value", [None, "", "a/b", "a b", "../x", "x" * 129])
def test_identifier_rejects_unsafe_path_segments(value):
    with pytest.raises(VenueError, match="^INVALID_IDENTIFIER$"):
        common.identifier(value)


@pytest.mark.parametrize("validator,value", [
    (common.number, True), (common.number, "1"), (common.number, 1.5),
    (common.string, ""), (common.string, None),
    (common.object_, []), (common.array, {}),
])
def test_normalized_types_fail_closed(validator, value):
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        validator(value)


@pytest.mark.parametrize("changes", [
    {"time": -1}, {"low": "0"}, {"high": "0.9"}, {"volume": Decimal("-1")},
])
def test_invalid_candle_rejected_at_adapter(changes):
    values = {"time": 10, "open": "1", "high": "2", "low": "0.5", "close": "1.5",
              "volume": Decimal(1), **changes}
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        common.bar(values["time"], values, volume=values["volume"])


def test_redirect_hook_returns_original_exception_without_redirect():
    failure = RuntimeError("PRIVATE_REDIRECT")
    assert common.NoRedirectConnect.process_redirect(None, failure) is failure


async def test_pacing_isolated_buckets_and_reservations(monkeypatch):
    wait = AsyncMock()
    monkeypatch.setattr(common, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(common, "_next_request", {})
    monkeypatch.setattr(common, "asyncio", SimpleNamespace(sleep=wait))
    await common.pace("a", 0.25)
    await common.pace("b", 0.25)
    await common.pace("a", 0.25)
    await common.pace("a", 0.25)
    assert [call.args[0] for call in wait.await_args_list] == [0.25, 0.5]
    assert common._next_request == {"a": 100.75, "b": 100.25}


@pytest.mark.parametrize("status,mutation,code,retryable,unknown", [
    (401, False, "AUTH_ERROR", False, False), (403, True, "AUTH_ERROR", False, False),
    (404, False, "NOT_FOUND", False, False), (400, True, "UPSTREAM_REJECTED", False, False),
    (409, True, "ORDER_CONFLICT", False, True), (409, False, "UPSTREAM_REJECTED", False, False),
    (302, False, "REDIRECT_BLOCKED", False, False), (307, True, "REDIRECT_BLOCKED", False, True),
    (429, False, "RATE_LIMIT", True, False), (429, True, "RATE_LIMIT", False, False),
    (408, False, "UPSTREAM_UNAVAILABLE", True, False), (425, True, "UPSTREAM_UNAVAILABLE", False, True),
    (503, True, "UPSTREAM_UNAVAILABLE", False, True),
])
def test_status_classification(status, mutation, code, retryable, unknown):
    error = common.status_error(status, mutation=mutation, retry_after=2)
    assert (error.code, error.retryable, error.outcome_unknown) == (code, retryable, unknown)


@pytest.mark.parametrize("value,expected", [("0", 0), ("3", 3), ("999", 60), ("1000", None),
                                           ("-1", None), ("2.5", None), ("tomorrow", None), ("", None)])
def test_retry_after_bounded(value, expected):
    assert common._delay(httpx.Response(429, headers={"Retry-After": value})) == expected


@pytest.fixture
def transport_clock(monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(common, "asyncio", SimpleNamespace(sleep=pause))
    return pause


@pytest.mark.parametrize("kind", ["rate", "timeout", "unavailable"])
async def test_read_retries_are_bounded_and_successful(transport_clock, kind):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            if kind == "timeout":
                raise httpx.ReadTimeout("PRIVATE_TIMEOUT", request=request)
            return httpx.Response(429 if kind == "rate" else 503, headers={"Retry-After": "1"})
        return httpx.Response(200, content=b'{"balance":1.25}')

    result = await common.request_json("GET", URL, transport=httpx.MockTransport(respond))
    assert result == {"balance": Decimal("1.25")}
    assert len(calls) == 3 and transport_clock.await_count == 2


@pytest.mark.parametrize("kind,code,unknown", [
    ("redirect", "REDIRECT_BLOCKED", True), ("timeout", "DISCONNECTED", True),
    ("duplicate", "INVALID_RESPONSE", True), ("conflict", "ORDER_CONFLICT", True),
    ("unavailable", "UPSTREAM_UNAVAILABLE", True),
])
async def test_mock_mutation_never_retries_or_follows_redirect(transport_clock, kind, code, unknown):
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TIMEOUT", request=request)
        if kind == "duplicate":
            return httpx.Response(200, content=b'{"id":"a","id":"b"}')
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid/secret"})
        return httpx.Response(409 if kind == "conflict" else 503)

    with pytest.raises(VenueError, match=f"^{code}$") as caught:
        await common.request_json("POST", URL, mutation=True, transport=httpx.MockTransport(respond),
                                  auth=lambda: {"Authorization": "Bearer fixture-only"})
    assert caught.value.outcome_unknown is unknown and not caught.value.retryable
    assert len(calls) == 1 and calls[0].url.host == "fixture.invalid"
    transport_clock.assert_not_awaited()


@pytest.mark.parametrize("kind", ["rate-limit-long", "exhausted", "malformed"])
async def test_read_stops_on_retry_budget_or_bad_payload(transport_clock, kind):
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "malformed":
            return httpx.Response(200, content=b'{"n":NaN}')
        return httpx.Response(429, headers={"Retry-After": "6" if kind == "rate-limit-long" else "1"})

    with pytest.raises(VenueError, match="^INVALID_RESPONSE$" if kind == "malformed" else "^RATE_LIMIT$"):
        await common.request_json("GET", URL, transport=httpx.MockTransport(respond))
    assert len(calls) == (3 if kind == "exhausted" else 1)
    assert transport_clock.await_count == (2 if kind == "exhausted" else 0)


@pytest.mark.parametrize("failure,code,retryable", [
    (InvalidStatus(SimpleNamespace(status_code=403)), "AUTH_ERROR", False),
    (InvalidStatus(SimpleNamespace(status_code=302)), "REDIRECT_BLOCKED", False),
    (WebSocketException("PRIVATE_SOCKET"), "DISCONNECTED", True),
    (OSError("PRIVATE_SOCKET"), "DISCONNECTED", True),
    (TimeoutError("PRIVATE_SOCKET"), "DISCONNECTED", True),
])
async def test_socket_failures_are_sanitized(monkeypatch, failure, code, retryable):
    monkeypatch.setattr(common, "pace", AsyncMock())

    @asynccontextmanager
    async def factory(*_args, **_kwargs):
        if failure is not None:
            raise failure
        yield None

    with pytest.raises(VenueError, match=f"^{code}$") as caught:
        async with common.socket_session("wss://fixture.invalid/ws", factory):
            pytest.fail("failed connection cannot yield")
    assert caught.value.retryable is retryable


async def test_socket_options_disable_proxy_and_receive_strict_json(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    socket = Mock()
    socket.recv = AsyncMock(side_effect=['{"value":0.25}', '{"x":1,"x":2}'])
    options = {}

    @asynccontextmanager
    async def factory(url, **kwargs):
        options.update(url=url, **kwargs)
        yield socket

    async with common.socket_session("wss://fixture.invalid/ws", factory, {"X-Fixture": "local"}) as ws:
        assert await common.receive(ws) == {"value": Decimal("0.25")}
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await common.receive(ws)
    assert options["proxy"] is None
    assert options["additional_headers"] == {"X-Fixture": "local"}
    assert options["logger"] is common._socket_logger


@pytest.mark.parametrize("method,mutation", [("POST", False), ("GET", True), ("DELETE", True)])
async def test_single_attempt_budget_for_non_read_or_mutation(transport_clock, method, mutation):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(VenueError, match="^UPSTREAM_UNAVAILABLE$") as caught:
        await common.request_json(method, URL, mutation=mutation, transport=httpx.MockTransport(respond))
    assert caught.value.outcome_unknown is mutation
    assert len(calls) == 1
    transport_clock.assert_not_awaited()


async def test_read_retry_default_backoff_and_fresh_auth(transport_clock):
    calls = []

    def respond(request):
        calls.append(request.headers["X-Fixture"])
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    signer = Mock(side_effect=[{"X-Fixture": "1"}, {"X-Fixture": "2"}, {"X-Fixture": "3"}])
    assert await common.request_json("GET", URL, auth=signer, transport=httpx.MockTransport(respond)) == {"ok": True}
    assert calls == ["1", "2", "3"]
    assert signer.call_count == 3
    assert [call.args[0] for call in transport_clock.await_args_list] == [0.5, 1.0]
