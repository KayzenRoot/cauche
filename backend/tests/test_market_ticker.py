"""CD-17: deterministic ticker lifecycle and stream integrity without network."""

import asyncio
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app import market_data as market
from app.integrations import Deriv, Kalshi, VenueError

NOW = 1800000000


def tick(instrument="MOCK", source="DERIV_PUBLIC", **overrides):
    return {"instrument": instrument, "source": source, "observed_at": NOW,
            "price": Decimal("0.42"), **overrides}


class ScriptedFeed:
    def __init__(self, rows, *, ending=None):
        self.rows = rows
        self.ending = ending
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.calls = []

    async def stream(self, instrument):
        self.calls.append(instrument)
        try:
            for row in self.rows:
                yield row
            self.ready.set()
            if self.ending == "eof":
                return
            if isinstance(self.ending, Exception):
                raise self.ending
            await asyncio.Event().wait()
        finally:
            self.closed.set()


def deriv(feed):
    venue = Mock(spec=Deriv)
    venue.stream_ticks = feed.stream
    return venue


def kalshi(feed, source="KALSHI_DEMO"):
    venue = Mock(spec=Kalshi)
    venue.source = source
    venue.stream_ticker = feed.stream
    return venue


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = Mock(return_value=NOW)
    monkeypatch.setattr(market, "epoch", clock)
    return clock


async def ready(event):
    await asyncio.wait_for(event.wait(), timeout=2)


@pytest.mark.parametrize("kind", ["deriv", "kalshi"])
async def test_live_mark_is_scoped_copied_and_expires(clock, kind):
    source = "DERIV_PUBLIC" if kind == "deriv" else "KALSHI_DEMO"
    feed = ScriptedFeed([tick(source=source)])
    venue = deriv(feed) if kind == "deriv" else kalshi(feed)
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", venue, "MOCK")
        await ready(feed.ready)
        assert ticker.latest("other") is None
        mark = ticker.latest("run")
        assert mark == {"price": "0.42", "observed_at": NOW, "source": source}
        mark["price"] = "bad"
        assert ticker.latest("run")["price"] == "0.42"
        # Repeated worker ticks construct fresh equivalent adapter instances.
        await ticker.select("run", deriv(feed) if kind == "deriv" else kalshi(feed), "MOCK")
        assert feed.calls == ["MOCK"] and ticker.latest("run") is not None
        clock.return_value = NOW + 15
        assert ticker.latest("run") is not None
        clock.return_value = NOW + 16
        assert ticker.latest("run") is None
        clock.return_value = NOW - 1
        assert ticker.latest("run") is None
    finally:
        await ticker.select(None)
    assert feed.closed.is_set() and ticker.latest("run") is None
    await ticker.select(None)


@pytest.mark.parametrize("change", ["run", "instrument", "source"])
async def test_subscription_change_cancels_previous_feed(change):
    old = ScriptedFeed([tick()])
    new_run = "next" if change == "run" else "run"
    new_instrument = "OTHER" if change == "instrument" else "MOCK"
    new_source = "KALSHI_DEMO" if change == "source" else "DERIV_PUBLIC"
    new = ScriptedFeed([tick(new_instrument, new_source, price=Decimal("0.51"))])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", deriv(old), "MOCK")
        await ready(old.ready)
        await ticker.select(new_run, kalshi(new) if change == "source" else deriv(new), new_instrument)
        assert old.closed.is_set()
        assert ticker.latest(new_run) is None
        await ready(new.ready)
        assert ticker.latest(new_run)["price"] == "0.51"
        if change == "run":
            assert ticker.latest("run") is None
    finally:
        await ticker.select(None)
    assert new.closed.is_set()


@pytest.mark.parametrize("ending", ["eof", VenueError("FIXTURE"), ValueError("FIXTURE"), OSError("FIXTURE")])
async def test_disconnect_clears_cache_and_reconnects(monkeypatch, ending):
    initial = ScriptedFeed([tick()], ending=ending)
    resumed = ScriptedFeed([tick(price=Decimal("0.55"))])
    venue = deriv(initial)
    retry = asyncio.Event()
    calls = []

    def subscribe(instrument):
        calls.append(instrument)
        return (initial if len(calls) == 1 else resumed).stream(instrument)

    async def pause(_seconds):
        retry.set()
        await allow.wait()

    allow = asyncio.Event()
    # Intercept only the retry boundary, never real time or network.
    monkeypatch.setattr(market, "asyncio", Mock(
        create_task=asyncio.create_task, gather=asyncio.gather, sleep=pause,
    ))
    venue.stream_ticks = subscribe
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", venue, "MOCK")
        await ready(retry)
        assert initial.closed.is_set() and ticker.latest("run") is None
        allow.set()
        await ready(resumed.ready)
        assert ticker.latest("run")["price"] == "0.55" and calls == ["MOCK", "MOCK"]
    finally:
        await ticker.select(None)
    assert resumed.closed.is_set()


@pytest.mark.parametrize("row", [
    {"status": "RECONNECTING"}, tick(observed_at=NOW - 16), tick(observed_at=NOW + 1),
    tick(observed_at=NOW - 1),
])
async def test_status_or_stale_or_out_of_order_tick_invalidates_mark(row):
    feed = ScriptedFeed([tick(), row])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", deriv(feed), "MOCK")
        await ready(feed.ready)
        assert ticker.latest("run") is None
    finally:
        await ticker.select(None)


@pytest.mark.parametrize("kind,row", [
    ("deriv", None), ("deriv", tick(source="KALSHI_DEMO")),
    ("deriv", tick(instrument="OTHER")), ("deriv", tick(observed_at=True)),
    ("deriv", tick(observed_at=-1)), ("deriv", tick(observed_at=str(NOW))),
    ("deriv", tick(price=False)), ("deriv", tick(price=0.42)),
    ("deriv", tick(price="NaN")), ("deriv", tick(price="Infinity")),
    ("deriv", tick(price="0")), ("deriv", tick(price="-1")),
    ("kalshi", tick(source="KALSHI_DEMO", price="1.01")),
    ("kalshi", tick(source="KALSHI_DEMO", price="-0.01")),
])
async def test_malformed_tick_clears_mark_and_closes_stream(kind, row):
    source = "DERIV_PUBLIC" if kind == "deriv" else "KALSHI_DEMO"
    feed = ScriptedFeed([tick(source=source), row])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", deriv(feed) if kind == "deriv" else kalshi(feed), "MOCK")
        await ready(feed.closed)
        assert ticker.latest("run") is None
    finally:
        await ticker.select(None)


@pytest.mark.parametrize("price", ["0", "1"])
async def test_kalshi_endpoint_marks_are_display_only(price):
    feed = ScriptedFeed([tick(source="KALSHI_DEMO", price=price)])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", kalshi(feed), "MOCK")
        await ready(feed.ready)
        assert ticker.latest("run")["price"] == price
    finally:
        await ticker.select(None)


@pytest.mark.parametrize("kind", ["unknown", "public-kalshi", "missing-instrument"])
async def test_invalid_subscription_clears_previous_scope(kind):
    old = ScriptedFeed([tick()])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", deriv(old), "MOCK")
        await ready(old.ready)
        venue = Mock() if kind == "unknown" else kalshi(old, "KALSHI_PUBLIC") if kind == "public-kalshi" else deriv(old)
        with pytest.raises(ValueError, match="INVALID_TICKER_CONTEXT"):
            await ticker.select("run", venue, "" if kind == "missing-instrument" else "MOCK")
        assert old.closed.is_set() and ticker.latest("run") is None
        assert old.calls == ["MOCK"]
    finally:
        await ticker.select(None)


async def test_completed_task_restarts_same_selection():
    first = ScriptedFeed([tick()])
    second = ScriptedFeed([tick(price="0.50")])
    ticker = market.MarketTicker()
    try:
        await ticker.select("run", deriv(first), "MOCK")
        await ready(first.ready)
        task = ticker._task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert first.closed.is_set() and ticker.latest("run") is None
        await ticker.select("run", deriv(second), "MOCK")
        await ready(second.ready)
        assert ticker.latest("run")["price"] == "0.50"
    finally:
        await ticker.select(None)


async def test_invalid_initial_selection_never_starts_stream():
    feed = ScriptedFeed([tick()])
    ticker = market.MarketTicker()
    with pytest.raises(ValueError, match="INVALID_TICKER_CONTEXT"):
        await ticker.select("run", deriv(feed), "")
    assert feed.calls == [] and ticker.latest("run") is None
    await ticker.select(None)
