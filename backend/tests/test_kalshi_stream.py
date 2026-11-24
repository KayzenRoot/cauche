"""CD-26: local Kalshi stream lifecycle, sequencing and destination guards."""

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.integrations import Kalshi, VenueError
from app.integrations import common, kalshi

NOW = 1800000000
SYMBOL = "FIXTURE"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(kalshi, "time", Mock(time=lambda: NOW, time_ns=lambda: NOW * 10**9))


@pytest.fixture(scope="module")
def key():
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


class Socket:
    def __init__(self, *rows):
        self.send = AsyncMock()
        self.recv = AsyncMock(side_effect=[
            json.dumps(row) if isinstance(row, dict) else row for row in rows
        ])


def venue(key, *sockets, public=False):
    opened, closed = [], []
    remaining = iter(sockets)

    @asynccontextmanager
    async def connect(url, **kwargs):
        assert url in {kalshi._DEMO_WS, kalshi._USER_WS}
        assert kwargs["proxy"] is None
        assert kwargs["additional_headers"]["KALSHI-ACCESS-KEY"] == "fixture-key"
        opened.append(url)
        try:
            socket = next(remaining)
            if isinstance(socket, Exception):
                raise socket
            yield socket
        finally:
            closed.append(url)

    return Kalshi("fixture-key", key, public=public, _connect=connect), opened, closed


def mark(**overrides):
    return {"type": "ticker", "msg": {
        "market_ticker": SYMBOL, "price_dollars": "0.42", "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.44", "yes_bid_size_fp": "1.25", "yes_ask_size_fp": "2",
        "ts_ms": NOW * 1000, **overrides,
    }}


def snapshot(seq=1, **overrides):
    return {"type": "orderbook_snapshot", "seq": seq, "msg": {
        "market_ticker": SYMBOL, "yes_dollars_fp": [["0.42", "4"]],
        "no_dollars_fp": [["0.55", "3"]], **overrides,
    }}


def delta(seq=2, **overrides):
    return {"type": "orderbook_delta", "seq": seq, "msg": {
        "market_ticker": SYMBOL, "side": "no", "price_dollars": "0.55",
        "delta_fp": "1", **overrides,
    }}


def fill(**overrides):
    return {"type": "fill", "msg": {
        "order_id": "order-1", "ticker": SYMBOL, "fill_id": "fill-1",
        "outcome_side": "yes", "count_fp": "1.25", "yes_price_dollars": "0.42",
        "no_price_dollars": "0.58", "is_taker": True, "fee_cost": "0.0012",
        "ts": NOW, **overrides,
    }}


@pytest.mark.parametrize("channel", ["ticker", "orderbook", "user_fills"])
async def test_public_streams_never_open_authenticated_socket(key, channel):
    adapter, opened, _ = venue(key, public=True)
    stream = getattr(adapter, "stream_" + channel)(SYMBOL)
    try:
        with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
            await anext(stream)
    finally:
        await stream.aclose()
    assert not opened


@pytest.mark.parametrize("url,channel", [
    ("wss://fixture.invalid/ws", "ticker"), (kalshi._DEMO_WS, "fill"),
    (kalshi._USER_WS, "ticker"), (kalshi._USER_WS, "unknown"),
])
async def test_private_stream_rejects_wrong_destination_before_signing(key, url, channel):
    adapter, opened, _ = venue(key)
    adapter._headers = Mock(side_effect=AssertionError("must not sign"))
    stream = adapter._stream(url, channel, SYMBOL)
    try:
        with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
            await anext(stream)
    finally:
        await stream.aclose()
    adapter._headers.assert_not_called()
    assert not opened


async def test_ticker_ignores_control_and_normalizes_exact_mark(key):
    socket = Socket({"type": "subscribed"}, mark())
    adapter, opened, closed = venue(key, socket)
    stream = adapter.stream_ticker(SYMBOL)
    try:
        row = await anext(stream)
        assert row == {"instrument": SYMBOL, "source": "KALSHI_DEMO", "observed_at": NOW,
                       "price": Decimal("0.42"), "yes_bid": Decimal("0.40"), "yes_ask": Decimal("0.44"),
                       "yes_bid_size": Decimal("1.25"), "yes_ask_size": Decimal("2")}
        assert json.loads(socket.send.await_args.args[0])["params"] == {
            "channels": ["ticker"], "market_tickers": [SYMBOL],
        }
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._DEMO_WS]


@pytest.mark.parametrize("changes", [
    {"market_ticker": "OTHER"}, {"yes_bid_dollars": "0.50"},
    {"price_dollars": "1.01"}, {"yes_ask_size_fp": "-1"},
    {"ts_ms": 0}, {"ts_ms": -1}, {"ts_ms": True}, {"ts_ms": (NOW + 1) * 1000},
])
async def test_invalid_ticker_closes_socket(key, changes):
    adapter, opened, closed = venue(key, Socket(mark(**changes)))
    stream = adapter.stream_ticker(SYMBOL)
    try:
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._DEMO_WS]


@pytest.mark.parametrize("message,code", [
    ({"type": "error", "msg": "fixture-only"}, "SUBSCRIPTION_REJECTED"),
    ({"status": "DISCONNECTED", "type": "ticker"}, "INVALID_RESPONSE"),
    ('{"type":"ticker","type":"fill"}', "INVALID_RESPONSE"),
])
async def test_remote_errors_cannot_masquerade_as_local_status(key, message, code):
    adapter, opened, closed = venue(key, Socket(message))
    stream = adapter.stream_ticker(SYMBOL)
    try:
        with pytest.raises(VenueError, match=f"^{code}$") as caught:
            await anext(stream)
        assert not caught.value.retryable
    finally:
        await stream.aclose()
    assert opened == closed


async def test_ticker_retry_backoff_and_reset_after_valid_message(key, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(kalshi.asyncio, "sleep", sleep)
    adapter, opened, closed = venue(key, OSError("fixture"), OSError("fixture"),
                                    Socket(mark(), OSError("fixture")), Socket(mark()))
    stream = adapter.stream_ticker(SYMBOL)
    try:
        assert (await anext(stream))["status"] == "DISCONNECTED"
        assert (await anext(stream))["status"] == "DISCONNECTED"
        assert (await anext(stream))["price"] == Decimal("0.42")
        assert (await anext(stream))["status"] == "DISCONNECTED"
        assert (await anext(stream))["price"] == Decimal("0.42")
        assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 1]
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._DEMO_WS] * 4


async def test_user_fill_stream_isolates_instrument_and_reports_disconnect(key, monkeypatch):
    monkeypatch.setattr(kalshi.asyncio, "sleep", AsyncMock())
    adapter, opened, closed = venue(key, OSError("fixture"),
                                    Socket({"type": "subscribed"}, fill(ticker="OTHER"), fill()))
    stream = adapter.stream_user_fills(SYMBOL)
    try:
        assert (await anext(stream))["status"] == "DISCONNECTED"
        row = await anext(stream)
        assert row["instrument"] == SYMBOL and row["quantity"] == Decimal("1.25")
        assert row["fee"] == Decimal("0.0012")
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._USER_WS] * 2


@pytest.mark.parametrize("message", [
    snapshot(0), snapshot(-1), snapshot(True), snapshot(market_ticker="OTHER"),
    snapshot(yes_dollars_fp=None), snapshot(no_dollars_fp=None),
    snapshot(ts_ms=0), snapshot(ts_ms=(NOW + 1) * 1000),
])
async def test_bad_snapshot_rejected(key, message):
    adapter, opened, closed = venue(key, Socket(message))
    stream = adapter.stream_orderbook(SYMBOL)
    try:
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
    finally:
        await stream.aclose()
    assert opened == closed


@pytest.mark.parametrize("message", [
    delta(side="invalid"), delta(side=[]), delta(delta_fp="-4"),
    delta(delta_fp="0.001"), delta(market_ticker="OTHER"),
    snapshot(1), snapshot(0),
])
async def test_invalid_update_does_not_publish_book(key, message):
    adapter, opened, closed = venue(key, Socket(snapshot(), message))
    stream = adapter.stream_orderbook(SYMBOL)
    try:
        assert (await anext(stream))["yes_ask_size"] == 3
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await anext(stream)
    finally:
        await stream.aclose()
    assert opened == closed


async def test_orderbook_removal_gap_and_resnapshot(key, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(kalshi.asyncio, "sleep", sleep)
    first = Socket({"type": "subscribed"}, snapshot(), delta(delta_fp="-3"), delta(4))
    second = Socket(snapshot(10, no_dollars_fp=[]))
    adapter, opened, closed = venue(key, first, second)
    stream = adapter.stream_orderbook(SYMBOL)
    try:
        assert (await anext(stream))["yes_ask_size"] == 3
        removed = await anext(stream)
        assert removed["yes_ask"] is None and removed["yes_ask_size"] == 0
        assert (await anext(stream))["status"] == "BOOK_SEQUENCE_GAP"
        assert (await anext(stream))["yes_ask"] is None
        sleep.assert_awaited_once_with(1)
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._DEMO_WS] * 2


async def test_orderbook_disconnect_requires_new_snapshot(key, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(kalshi.asyncio, "sleep", sleep)
    adapter, opened, closed = venue(key, Socket(snapshot(), OSError("fixture")), Socket(delta()),
                                    Socket(snapshot(10)))
    stream = adapter.stream_orderbook(SYMBOL)
    try:
        assert (await anext(stream))["yes_ask_size"] == 3
        assert (await anext(stream))["status"] == "DISCONNECTED"
        assert (await anext(stream))["status"] == "BOOK_SEQUENCE_GAP"
        assert (await anext(stream))["yes_ask_size"] == 3
        assert sleep.await_count == 2
    finally:
        await stream.aclose()
    assert opened == closed == [kalshi._DEMO_WS] * 3
