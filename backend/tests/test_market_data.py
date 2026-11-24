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


# CD-16: Kalshi normalized orderbook, provenance and bounded observation age.
PRIMARY = "MOCK-PRIMARY"
PEER = "MOCK-PEER"
EVENT = "MOCK-EVENT"


def kalshi_meta(instrument=PRIMARY, source="KALSHI_DEMO", **overrides):
    return {
        "instrument": instrument, "event_ticker": EVENT, "source": source,
        "status": "active", "close_time": NOW + 300, "settled_at": None,
        "result": None, "price_ranges": [{"start": D("0"), "end": D("1"), "step": D("0.01")}],
        "maker_fee_rate": D("0"), "taker_fee_rate": D("0.07"),
        "rules_primary": "fixture rules", "rules_secondary": "fixture terms",
        "no_ask": D("0.60"), **overrides,
    }


def kalshi_levels(instrument=PRIMARY, source="KALSHI_DEMO", **overrides):
    return {
        "instrument": instrument, "event": EVENT, "source": source, "observed_at": NOW,
        "yes_bid": D("0.40"), "yes_ask": D("0.42"),
        "yes_bid_size": D("20"), "yes_ask_size": D("21"), **overrides,
    }


@pytest.fixture
def kalshi(monkeypatch):
    adapter = Mock()
    adapter.market = AsyncMock(return_value=kalshi_meta())
    adapter.candles = AsyncMock(return_value=[bar(NOW - 300)])
    adapter.orderbook = AsyncMock(return_value=kalshi_levels())
    adapter.exclusive_siblings = AsyncMock(return_value=[])
    constructor = Mock(return_value=adapter)
    monkeypatch.setattr(market, "Kalshi", constructor)
    monkeypatch.setattr(market, "epoch", lambda: NOW)
    return adapter, constructor


def test_book_exact_fees_and_zero_rate_verified():
    book = market.normalize_book(kalshi_meta(), kalshi_levels(yes_bid="0.40", yes_ask="0.42"))
    assert book.valid() and book.fee_schedule_verified
    assert book.maker_fee_rate == D("0") and book.taker_fee_rate == D("0.07")
    assert book.yes_bid_size == D("20") and book.tick == D("0.01")


@pytest.mark.parametrize("field", ["yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size"])
def test_book_missing_level_is_unorderable(field):
    assert market.normalize_book(kalshi_meta(), kalshi_levels(**{field: None})) is None


@pytest.mark.parametrize("ranges", [None, [], {}, [{"step": "0.01"}, {"step": "0.02"}]])
def test_book_unsupported_grid_is_unorderable(ranges):
    assert market.normalize_book(kalshi_meta(price_ranges=ranges), kalshi_levels()) is None


@pytest.mark.parametrize("field,value", [
    ("yes_bid", True), ("yes_ask", 0.42), ("yes_bid_size", "NaN"), ("yes_ask_size", "Infinity"),
    ("yes_bid", "invalid"), ("yes_bid", {}), ("yes_ask", []),
])
def test_book_malformed_amount_rejected(field, value):
    with pytest.raises(ValueError, match="INVALID_MARKET_BOOK"):
        market.normalize_book(kalshi_meta(), kalshi_levels(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("yes_bid", "0"), ("yes_bid", "0.43"), ("yes_ask", "1"),
    ("yes_ask", "0.421"), ("yes_bid_size", "-1"), ("yes_ask_size", "0"),
])
def test_book_economically_invalid_levels_unorderable(field, value):
    assert market.normalize_book(kalshi_meta(), kalshi_levels(**{field: value})) is None


@pytest.mark.parametrize("meta,levels", [(None, {}), ({}, []), ({"price_ranges": [None]}, kalshi_levels())])
def test_book_invalid_envelope(meta, levels):
    with pytest.raises(ValueError, match="INVALID_MARKET_BOOK"):
        market.normalize_book(meta, levels)


@pytest.mark.parametrize("fee", ["maker_fee_rate", "taker_fee_rate"])
def test_book_unknown_fee_not_verified(fee):
    book = market.normalize_book(kalshi_meta(**{fee: None}), kalshi_levels())
    assert book.valid() and not book.fee_schedule_verified


@pytest.mark.parametrize("demo", [True, False])
async def test_kalshi_observe_routes_source_and_native_market(kalshi, demo):
    adapter, constructor = kalshi
    source = "KALSHI_DEMO" if demo else "KALSHI_PUBLIC"
    adapter.market.return_value = kalshi_meta(source=source)
    adapter.orderbook.return_value = kalshi_levels(source=source)
    result = await market.observe("BROKER_DEMO" if demo else "PAPER", "KALSHI", PRIMARY, 0, {})
    constructor.assert_called_once_with(public=not demo)
    adapter.market.assert_awaited_once_with(PRIMARY)
    adapter.candles.assert_awaited_once_with(PRIMARY, 300, 120)
    adapter.orderbook.assert_awaited_once_with(PRIMARY, market=adapter.market.return_value)
    assert result.instrument == PRIMARY and result.event == EVENT
    assert result.source == ("KALSHI_OFFICIAL_DEMO_MARKET" if demo else "KALSHI_OFFICIAL_PUBLIC")
    assert result.book.valid() and result.timestamp == NOW
    assert result.relation is None


@pytest.mark.parametrize("field,value", [
    ("instrument", PEER), ("source", "KALSHI_PUBLIC"), ("event_ticker", ""),
    ("event_ticker", None), ("close_time", None), ("close_time", True), ("close_time", -1),
])
async def test_kalshi_invalid_market_identity_never_reads_book(kalshi, field, value):
    adapter, _ = kalshi
    adapter.market.return_value[field] = value
    with pytest.raises(ValueError, match="INVALID_MARKET_CONTEXT"):
        await market.kalshi_observation(PRIMARY, demo=True)
    adapter.orderbook.assert_not_awaited()


@pytest.mark.parametrize("field,value,error", [
    ("instrument", PEER, "INVALID_MARKET_CONTEXT"), ("event", "OTHER", "INVALID_MARKET_CONTEXT"),
    ("source", "KALSHI_PUBLIC", "INVALID_MARKET_CONTEXT"), ("observed_at", True, "INVALID_MARKET_CONTEXT"),
    ("observed_at", "1800000000", "INVALID_MARKET_CONTEXT"), ("observed_at", NOW - 16, "STALE_MARKET_DATA"),
    ("observed_at", NOW + 1, "STALE_MARKET_DATA"),
])
async def test_kalshi_invalid_book_identity_or_age(kalshi, field, value, error):
    adapter, _ = kalshi
    adapter.orderbook.return_value[field] = value
    with pytest.raises(ValueError, match=error):
        await market.kalshi_observation(PRIMARY, demo=True)
    adapter.exclusive_siblings.assert_not_awaited()


@pytest.mark.parametrize("status,result,settled_at,close,expected", [
    ("settled", "YES", NOW, NOW - 1, True),
    ("finalized", "NO", NOW, NOW - 1, False),
    ("settled", "SCALAR", NOW, NOW - 1, None),
    ("closed", None, None, NOW - 1, None),
    ("active", None, None, NOW, None),
    ("active", "YES", NOW, NOW + 300, True),
])
async def test_kalshi_terminal_or_expired_never_reads_executable_depth(kalshi, status, result, settled_at, close, expected):
    adapter, _ = kalshi
    adapter.market.return_value.update(status=status, result=result, settled_at=settled_at, close_time=close)
    value = await market.kalshi_observation(PRIMARY, demo=True, scanner=True)
    assert value.outcome is expected and value.book is None and value.relation is None
    adapter.orderbook.assert_not_awaited()
    adapter.exclusive_siblings.assert_not_awaited()


async def test_kalshi_book_age_boundary_excludes_candle_lookahead(kalshi):
    adapter, _ = kalshi
    adapter.orderbook.return_value["observed_at"] = NOW - 15
    adapter.candles.return_value = [bar(NOW - 600), bar(NOW - 300)]
    value = await market.kalshi_observation(PRIMARY, demo=True)
    assert value.timestamp == NOW - 15
    assert [c.time for c in value.candles] == [NOW - 300]


@pytest.mark.parametrize("clock", [NOW + 16, NOW - 1])
async def test_kalshi_final_freshness_rechecked(kalshi, monkeypatch, clock):
    monkeypatch.setattr(market, "epoch", Mock(side_effect=[NOW, NOW, clock]))
    with pytest.raises(ValueError, match="STALE_MARKET_DATA"):
        await market.kalshi_observation(PRIMARY, demo=True)


async def test_scanner_refreshes_primary_and_binds_verified_peer(kalshi):
    adapter, _ = kalshi
    peer = kalshi_meta(PEER)
    adapter.exclusive_siblings.return_value = [peer]
    fresh = kalshi_levels(yes_bid="0.41", yes_ask="0.43")
    adapter.orderbook.side_effect = [kalshi_levels(), kalshi_levels(PEER), fresh]
    value = await market.observe("BROKER_DEMO", "KALSHI", PRIMARY, 0, {}, strategy="KALSHI-STRAT-03")
    assert value.book.yes_bid == D("0.41") and value.relation.verified
    assert value.relation.other_instrument == PEER
    assert value.rules_id == value.relation.rules_id and len(value.rules_id) == 64
    assert adapter.orderbook.await_args_list == [
        call(PRIMARY, market=adapter.market.return_value), call(PEER, market=peer),
        call(PRIMARY, market=adapter.market.return_value),
    ]


@pytest.mark.parametrize("kind", ["wrong-event", "wrong-source", "same-instrument", "wrong-close", "closed", "no-depth", "unknown-fees"])
async def test_scanner_invalid_peer_never_qualifies(kalshi, kind):
    adapter, _ = kalshi
    peer = kalshi_meta(PEER)
    overrides = {"wrong-event": {"event_ticker": "OTHER"}, "wrong-source": {"source": "KALSHI_PUBLIC"},
                 "same-instrument": {"instrument": PRIMARY}, "wrong-close": {"close_time": NOW + 301},
                 "closed": {"status": "closed"}}
    if kind in overrides:
        peer.update(overrides[kind])
    adapter.exclusive_siblings.return_value = [peer]
    if kind == "unknown-fees":
        adapter.market.return_value["maker_fee_rate"] = None
    peer_levels = kalshi_levels(PEER, yes_bid=None if kind == "no-depth" else D("0.40"))
    adapter.orderbook.side_effect = (
        [kalshi_levels(), kalshi_levels()] if kind in overrides
        else [kalshi_levels(), peer_levels, kalshi_levels()]
    )
    value = await market.kalshi_observation(PRIMARY, demo=True, scanner=True)
    assert value.relation is None and value.rules_id == ""
    assert adapter.orderbook.await_count == (2 if kind in overrides else 3)


@pytest.mark.parametrize("error", [VenueError("MOCK_UNAVAILABLE"), ValueError("invalid"), TypeError("invalid"), KeyError("invalid")])
async def test_scanner_discovery_failure_still_refreshes_primary(kalshi, error):
    adapter, _ = kalshi
    adapter.exclusive_siblings.side_effect = error
    adapter.orderbook.side_effect = [kalshi_levels(), kalshi_levels(yes_bid="0.41", yes_ask="0.43")]
    value = await market.kalshi_observation(PRIMARY, demo=True, scanner=True)
    assert value.relation is None and value.book.yes_bid == D("0.41")
    assert adapter.orderbook.await_count == 2


async def test_scanner_refresh_failure_does_not_reuse_old_primary(kalshi):
    adapter, _ = kalshi
    adapter.orderbook.side_effect = [kalshi_levels(), VenueError("MOCK_REFRESH_FAILED", retryable=True)]
    with pytest.raises(VenueError, match="MOCK_REFRESH_FAILED"):
        await market.kalshi_observation(PRIMARY, demo=True, scanner=True)
    assert adapter.orderbook.await_count == 2


@pytest.mark.parametrize("kind", ["stale-peer", "future-peer", "no-primary-depth", "market-closed"])
async def test_scanner_discards_relation_when_final_snapshot_incompatible(kalshi, monkeypatch, kind):
    adapter, _ = kalshi
    adapter.exclusive_siblings.return_value = [kalshi_meta(PEER)]
    if kind == "market-closed":
        adapter.market.return_value["close_time"] = NOW + 1
        adapter.exclusive_siblings.return_value[0]["close_time"] = NOW + 1
    peer_time = NOW - 15 if kind == "stale-peer" else NOW
    primary_time = NOW - 1 if kind == "future-peer" else NOW
    adapter.orderbook.side_effect = [
        kalshi_levels(), kalshi_levels(PEER, observed_at=peer_time),
        kalshi_levels(observed_at=primary_time, yes_bid=None if kind == "no-primary-depth" else D("0.40")),
    ]
    final_now = NOW + 1 if kind in {"stale-peer", "market-closed"} else NOW
    monkeypatch.setattr(market, "epoch", Mock(side_effect=[NOW, NOW, NOW, NOW, final_now]))
    value = await market.kalshi_observation(PRIMARY, demo=True, scanner=True)
    assert value.relation is None and value.rules_id == ""
    if kind in {"no-primary-depth", "market-closed"}:
        assert value.book is None
