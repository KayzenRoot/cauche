"""CD-20: Deriv RPC correlation, public market reads and stream boundaries."""

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.integrations import Deriv, VenueError
from app.integrations import common, deriv

NOW = 1800000000
SYMBOL = "frxEURUSD"


@pytest.fixture(autouse=True)
def isolated_clock(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(deriv, "pace", AsyncMock())
    monkeypatch.setattr(deriv, "time", Mock(time=lambda: NOW))


class Socket:
    def __init__(self, *responses):
        self.send = AsyncMock()
        self.recv = AsyncMock(side_effect=[
            json.dumps(row) if isinstance(row, dict) else row for row in responses
        ])


def rpc_reply(command, result, **overrides):
    return {"req_id": 1, "msg_type": command, command: result, **overrides}


def public_adapter(*sockets):
    opened = []
    closed = []
    remaining = iter(sockets)

    @asynccontextmanager
    async def connect(url, **kwargs):
        opened.append(url)
        assert url == "wss://api.derivws.com/trading/v1/options/ws/public"
        assert kwargs["additional_headers"] is None and kwargs["proxy"] is None
        socket = next(remaining)
        try:
            if isinstance(socket, Exception):
                raise socket
            yield socket
        finally:
            closed.append(url)

    return Deriv(_connect=connect), opened, closed


@pytest.mark.parametrize("mutation", [False, True])
@pytest.mark.parametrize("response,code,retry,unknown", [
    ('{"req_id":1,"req_id":2}', "INVALID_RESPONSE", False, True),
    ({"req_id": True}, "UNCORRELATED_RESPONSE", False, True),
    ({"req_id": 2}, "UNCORRELATED_RESPONSE", False, True),
    ({"req_id": 1, "error": "private-detail"}, "INVALID_RESPONSE", False, True),
    ({"req_id": 1, "error": {"code": "InvalidToken"}}, "AUTH_ERROR", False, False),
    ({"req_id": 1, "error": {"code": "RateLimit"}}, "RATE_LIMIT", True, False),
    ({"req_id": 1, "error": {"code": "PriceMoved"}}, "UPSTREAM_REJECTED", False, False),
    ({"req_id": 1, "error": {"code": "UnknownFixture"}}, "UPSTREAM_REJECTED", False, True),
    ({"req_id": 1, "msg_type": "other"}, "INVALID_RESPONSE", False, True),
    ({"req_id": 1, "msg_type": "fixture"}, "INVALID_RESPONSE", False, True),
])
async def test_rpc_errors_are_correlated_and_sanitized(mutation, response, code, retry, unknown):
    ws = Socket(response)
    with pytest.raises(VenueError, match=f"^{code}$") as caught:
        await Deriv()._rpc(ws, "fixture", {"fixture": 1}, mutation=mutation)
    assert caught.value.retryable is (retry and not mutation)
    assert caught.value.outcome_unknown is (unknown and mutation)
    ws.send.assert_awaited_once()
    ws.recv.assert_awaited_once()


async def test_interleaved_proposal_error_not_discarded():
    ws = Socket(
        {"req_id": 99, "msg_type": "proposal", "subscription": {"id": "p1"}, "error": {"code": "Fixture"}},
        rpc_reply("fixture", {"ok": True}),
    )
    with pytest.raises(VenueError, match="^UNCORRELATED_RESPONSE$") as caught:
        await Deriv()._rpc(ws, "fixture", {"fixture": 1}, mutation=True, proposal_stream="p1")
    assert caught.value.outcome_unknown
    ws.send.assert_awaited_once()
    ws.recv.assert_awaited_once()


async def test_subscription_identity_mismatch_rejected():
    ws = Socket(rpc_reply("proposal", {"id": "p1"}, subscription={"id": "p2"}))
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await Deriv()._rpc(ws, "proposal", {"proposal": 1, "subscribe": 1})


async def test_public_markets_and_contracts_normalization():
    symbols = [
        {"underlying_symbol": SYMBOL, "underlying_symbol_name": "EUR/USD", "market": "forex",
         "exchange_is_open": 1, "is_trading_suspended": 0},
        {"underlying_symbol": "frxGBPUSD", "underlying_symbol_name": "GBP/USD", "market": "forex",
         "exchange_is_open": 1, "is_trading_suspended": 1},
    ]
    ws = Socket(rpc_reply("active_symbols", symbols))
    adapter, opened, closed = public_adapter(ws)
    rows = await adapter.markets()
    assert [r["is_open"] for r in rows] == [True, False]
    assert all(r["source"] == "DERIV_PUBLIC" for r in rows)
    assert opened == closed
    request = json.loads(ws.send.await_args.args[0])
    assert request == {"active_symbols": "brief", "req_id": 1}
    item = {"underlying_symbol": SYMBOL, "contract_type": "CALL", "contract_category": "callput",
            "min_contract_duration": "3m", "max_contract_duration": "2h", "barriers": 1}
    ws = Socket(rpc_reply("contracts_for", {"available": [item, {**item, "underlying_symbol": "OTHER"}]}))
    adapter, opened, closed = public_adapter(ws)
    assert await adapter.contracts(SYMBOL) == [{
        "contract_type": "CALL", "category": "callput", "min_duration": "3m", "max_duration": "2h", "barriers": 1,
    }]
    assert opened == closed


@pytest.mark.parametrize("interval,limit", [(1, 200), (300, 0), (300, 1001)])
async def test_invalid_candle_range_before_socket(interval, limit):
    adapter, opened, _ = public_adapter()
    with pytest.raises(VenueError, match="^INVALID_HISTORY_RANGE$"):
        await adapter.candles(SYMBOL, interval, limit)
    assert opened == []


@pytest.mark.parametrize("limit", [0, 1001])
async def test_invalid_tick_history_limit_before_socket(limit):
    adapter, opened, _ = public_adapter()
    with pytest.raises(VenueError, match="^INVALID_HISTORY_RANGE$"):
        await adapter.history_ticks(SYMBOL, limit)
    assert opened == []


async def test_tick_history_preserves_same_second_ticks_and_exact_values():
    ws = Socket(rpc_reply("history", {"times": [NOW - 2, NOW - 2, NOW], "prices": ["1.00000001", "1.1", "1.2"]}))
    adapter, opened, closed = public_adapter(ws)
    rows = await adapter.history_ticks(SYMBOL, 3)
    assert [r["observed_at"] for r in rows] == [NOW - 2, NOW - 2, NOW]
    assert [r["price"] for r in rows] == list(map(Decimal, ["1.00000001", "1.1", "1.2"]))
    assert all(r["instrument"] == SYMBOL and r["source"] == "DERIV_PUBLIC" for r in rows)
    assert opened == closed
    assert json.loads(ws.send.await_args.args[0]) == {
        "ticks_history": SYMBOL, "end": "latest", "style": "ticks", "count": 3, "req_id": 1,
    }


@pytest.mark.parametrize("times,prices", [
    ([NOW], []), ([-1], ["1"]), ([True], ["1"]), ([NOW + 1], ["1"]),
    ([NOW, NOW - 1], ["1", "2"]), ([NOW], ["0"]), ([NOW], ["-1"]), ([NOW], ["NaN"]),
])
async def test_tick_history_rejects_invalid_series(times, prices):
    ws = Socket(rpc_reply("history", {"times": times, "prices": prices}))
    adapter, opened, closed = public_adapter(ws)
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await adapter.history_ticks(SYMBOL)
    assert opened == closed


def tick(**overrides):
    return {"symbol": SYMBOL, "epoch": NOW, "quote": "1.25", "bid": "1.24", "ask": "1.26", **overrides}


@pytest.mark.parametrize("bad", [
    {"symbol": "OTHER"}, {"epoch": -1}, {"epoch": True}, {"quote": "0"}, {"quote": "-1"}, {"quote": "NaN"},
])
async def test_invalid_first_stream_tick_closes_connection(bad):
    ws = Socket(rpc_reply("tick", tick(**bad)))
    adapter, opened, closed = public_adapter(ws)
    stream = adapter.stream_ticks(SYMBOL)
    try:
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
        assert opened == closed
    finally:
        await stream.aclose()


@pytest.mark.parametrize("bad", [
    {"msg_type": "other"}, {"msg_type": "tick", "tick": tick(), "error": {"code": "Fixture"}},
    {"msg_type": "tick", "tick": tick(symbol="OTHER")},
])
async def test_stream_rejects_invalid_update_after_valid_mark(bad):
    ws = Socket(rpc_reply("tick", tick()), bad)
    adapter, opened, closed = public_adapter(ws)
    stream = adapter.stream_ticks(SYMBOL)
    try:
        assert (await anext(stream))["price"] == Decimal("1.25")
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
        assert opened == closed
    finally:
        await stream.aclose()


async def test_stream_reconnects_after_transport_error_and_yields_new_mark(monkeypatch):
    first = Socket(rpc_reply("tick", tick()), {"msg_type": "tick", "tick": tick(quote="1.3")}, OSError("fixture"))
    second = Socket(rpc_reply("tick", tick(quote="1.5"), req_id=2))
    adapter, opened, closed = public_adapter(first, second)
    sleep = AsyncMock()
    monkeypatch.setattr(deriv.asyncio, "sleep", sleep)
    stream = adapter.stream_ticks(SYMBOL)
    try:
        assert (await anext(stream))["price"] == Decimal("1.25")
        assert (await anext(stream))["price"] == Decimal("1.3")
        assert await anext(stream) == {"status": "DISCONNECTED", "instrument": SYMBOL, "observed_at": NOW}
        assert len(opened) == len(closed) == 1
        assert (await anext(stream))["price"] == Decimal("1.5")
        sleep.assert_awaited_once_with(1)
        assert len(opened) == 2
        assert json.loads(second.send.await_args.args[0]) == {"ticks": SYMBOL, "subscribe": 1, "req_id": 2}
    finally:
        await stream.aclose()
    assert opened == closed
