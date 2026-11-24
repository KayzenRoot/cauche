"""CD-15: normalized candles and multi-timeframe freshness, no remote I/O."""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock, call

import pytest

from app import market_data as market
from app.integrations import VenueError

NOW = 1800000000
D = Decimal


def bar(opened, **overrides):
    return {
        "time": opened, "open": D("1.10"), "high": D("1.12"),
        "low": D("1.09"), "close": D("1.11"), "volume": D("2"),
        **overrides,
    }


def test_candles_sorted_closed_exact_and_idempotent_duplicates():
    first = bar(NOW - 900, volume=None)
    last = bar(NOW - 300, close="1.11000001", volume="0.25")
    rows = [last, bar(NOW), first, dict(last)]
    result = market.closed_candles(rows, 300, NOW)
    assert [c.time for c in result] == [NOW - 600, NOW]
    assert result[0].volume is None and result[1].volume == D("0.25")
    assert result[1].close == D("1.11000001")
    assert all(c.closed and c.valid() for c in result)
    # Missing intervals remain missing: no interpolation or fabricated candles.
    assert market.closed_candles([], 300, NOW) == ()
    assert market.closed_candles(rows[::-1], 300, NOW) == result


@pytest.mark.parametrize("field,value", [
    ("time", -1), ("time", True), ("time", "1800000000"), ("time", 1.25), ("time", None),
    ("open", True), ("open", 1.1), ("open", None), ("open", "NaN"), ("open", "Infinity"),
    ("open", "not-a-number"), ("open", "0"), ("high", "1.01"), ("low", "1.15"),
    ("close", "-1"), ("volume", "-0.01"), ("volume", "NaN"),
    ("volume", float("inf")), ("volume", False),
])
def test_invalid_candle_fields_are_rejected(field, value):
    with pytest.raises(ValueError, match="INVALID_MARKET_CANDLE"):
        market.closed_candles([bar(NOW - 300, **{field: value})], 300, NOW)


@pytest.mark.parametrize("rows,interval,now", [
    (None, 300, NOW), ({}, 300, NOW), ([None], 300, NOW),
    ([], 0, NOW), ([], -300, NOW), ([], True, NOW), ([], 300, True), ([], 300, -1),
])
def test_invalid_candle_envelope(rows, interval, now):
    with pytest.raises(ValueError, match="INVALID_MARKET_CANDLE"):
        market.closed_candles(rows, interval, now)


@pytest.mark.parametrize("overrides", [{"close": "1.105"}, {"volume": None}, {"volume": "3"}])
def test_conflicting_closed_duplicates_fail_in_both_orders(overrides):
    first, conflict = bar(NOW - 300), bar(NOW - 300, **overrides)
    for rows in ([first, conflict], [conflict, first]):
        with pytest.raises(ValueError, match="CONFLICTING_MARKET_CANDLE"):
            market.closed_candles(rows, 300, NOW)


@pytest.fixture
def deriv(monkeypatch):
    adapter = Mock()
    adapter.candles = AsyncMock(side_effect=[[bar(NOW - 300)], [bar(NOW - 900)]])
    adapter.quote = AsyncMock(return_value={"currency": "USD", "stake": D("1"), "payout": D("1.85")})
    constructor = Mock(return_value=adapter)
    monkeypatch.setattr(market, "Deriv", constructor)
    monkeypatch.setattr(market, "epoch", lambda: NOW)
    return adapter, constructor


@pytest.mark.parametrize("instrument,native", [
    ("EUR/USD", "frxEURUSD"), ("GBP/USD", "frxGBPUSD"), ("USD/JPY", "frxUSDJPY"), ("OTHER", "OTHER"),
])
async def test_deriv_fresh_observation_and_native_symbol(deriv, instrument, native):
    adapter, constructor = deriv
    result = await market.observe("PAPER", "DERIV", instrument, 0, {"stake": "1", "duration_seconds": 300})
    constructor.assert_called_once_with()
    assert adapter.candles.await_args_list == [call(native, 300, 110), call(native, 900, 90)]
    assert adapter.quote.await_args_list == [
        call(native, "CALL", D("1"), duration=5), call(native, "PUT", D("1"), duration=5),
    ]
    assert result.source == "DERIV_OFFICIAL_PUBLIC" and result.instrument == instrument
    assert result.event == native and result.timestamp == NOW
    assert result.candles[-1].time == result.confirmation[-1].time == NOW
    assert [q.side for q in result.contracts] == ["CALL", "PUT"]
    assert result.deriv_quote("CALL", D("1"), 300).payout == D("1.85")


@pytest.mark.parametrize("lower,higher", [
    ([], [bar(NOW - 900)]),
    ([bar(NOW - 616)], [bar(NOW - 900)]),
    ([bar(NOW)], [bar(NOW - 900)]),
    ([bar(NOW - 300)], []),
    ([bar(NOW - 300)], [bar(NOW - 1816)]),
    ([bar(NOW - 600)], [bar(NOW - 900)]),
])
async def test_deriv_missing_stale_or_lookahead_confirmation_blocks_quotes(deriv, lower, higher):
    adapter, _ = deriv
    adapter.candles.side_effect = [lower, higher]
    with pytest.raises(ValueError, match="STALE_MARKET_DATA"):
        await market.deriv_observation("EUR/USD", {"stake": "1", "duration_seconds": 300})
    adapter.quote.assert_not_awaited()


async def test_deriv_freshness_boundary_and_confirmation_no_lookahead(deriv):
    adapter, _ = deriv
    adapter.candles.side_effect = [
        [bar(NOW - 615)],
        [bar(NOW - 1815), bar(NOW - 900)],
    ]
    result = await market.deriv_observation("EUR/USD", {"stake": "1", "duration_seconds": 300})
    assert result.timestamp == NOW - 315
    assert [c.time for c in result.confirmation] == [NOW - 915]
    assert all(c.time <= result.timestamp for c in result.confirmation)


@pytest.mark.parametrize("duration", [30, 61])
async def test_deriv_unsupported_duration_stays_unorderable(deriv, duration):
    adapter, _ = deriv
    result = await market.deriv_observation("EUR/USD", {"stake": "1", "duration_seconds": duration})
    assert result.contracts == ()
    adapter.quote.assert_not_awaited()


async def test_deriv_quote_unavailable_or_wrong_currency_is_not_synthesized(deriv):
    adapter, _ = deriv
    adapter.quote.side_effect = [VenueError("MOCK_UNAVAILABLE"), {"currency": "EUR", "stake": D("1"), "payout": D("2")}]
    result = await market.deriv_observation("EUR/USD", {"stake": "1", "duration_seconds": 300})
    assert result.contracts == ()
    assert adapter.quote.await_count == 2


async def test_observe_unsupported_exchange_fails_without_adapter():
    with pytest.raises(ValueError, match="UNSUPPORTED_EXCHANGE"):
        await market.observe("PAPER", "OTHER", "OTHER", 0, {})


@pytest.mark.parametrize("after_quotes", [NOW + 316, NOW - 1])
async def test_deriv_rechecks_freshness_after_quote_reads(deriv, monkeypatch, after_quotes):
    adapter, _ = deriv
    clock = Mock(side_effect=[NOW, after_quotes])
    monkeypatch.setattr(market, "epoch", clock)
    with pytest.raises(ValueError, match="STALE_MARKET_DATA"):
        await market.deriv_observation("EUR/USD", {"stake": "1", "duration_seconds": 300})
    assert adapter.quote.await_count == 2
    assert clock.call_count == 2
