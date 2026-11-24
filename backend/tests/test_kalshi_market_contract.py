"""CD-24: Kalshi public market catalogue, history and depth contracts."""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.integrations import Kalshi, VenueError
from app.integrations import common, kalshi

NOW = 1800000000
ROOT = "/trade-api/v2"
TICKER = "FIXTURE"
EVENT = "FIXTURE-EVENT"
SERIES = "FIXTURE-SERIES"


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(kalshi, "time", Mock(time=lambda: NOW))


def market(ticker=TICKER, **overrides):
    return {"ticker": ticker, "event_ticker": EVENT, "market_type": "binary",
            "status": "active", "result": "", "close_time": "2099-12-31T00:00:00Z",
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}], **overrides}


def adapter(responses, *, public=True):
    calls = []
    def respond(request):
        assert request.method == "GET"
        assert request.url.host == ("external-api.kalshi.com" if public else "external-api.demo.kalshi.co")
        assert "KALSHI-ACCESS-KEY" not in request.headers
        path = request.url.path.removeprefix(ROOT)
        calls.append((path, dict(request.url.params)))
        response = responses[path]
        if callable(response):
            response = response(request)
        return httpx.Response(200, json=response)
    return Kalshi(public=public, _transport=httpx.MockTransport(respond)), calls


@pytest.mark.parametrize("method", ["markets", "related_markets"])
@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
async def test_invalid_catalogue_limit_before_transport(method, limit):
    venue, calls = adapter({})
    with pytest.raises(VenueError, match="^INVALID_LIMIT$"):
        if method == "markets":
            await venue.markets(limit=limit)
        else:
            await venue.related_markets(TICKER, limit=limit)
    assert not calls


async def test_catalogue_filters_nonbinary_and_limits_results():
    venue, calls = adapter({"/markets": {"markets": [
        market("SKIP", market_type="scalar"), market(), market("NEXT"),
    ], "cursor": "next-page"}})
    result = await venue.markets(limit=1)
    assert [row["instrument"] for row in result] == [TICKER]
    assert calls == [("/markets", {"status": "open", "limit": "1", "mve_filter": "exclude"})]


async def test_catalogue_category_walks_series_and_stops_at_limit():
    def rows(request):
        return {"markets": [market(request.url.params["series_ticker"])]}
    venue, calls = adapter({"/series": {"series": [
        {"ticker": SERIES, "category": "Test"}, {"ticker": "SECOND", "category": "Test"},
        {"ticker": "THIRD", "category": "Test"},
    ]}, "/markets": rows})
    result = await venue.markets(category="Test", limit=2)
    assert [(r["instrument"], r["category"], r["series_ticker"]) for r in result] == [
        (SERIES, "Test", SERIES), ("SECOND", "Test", "SECOND"),
    ]
    assert [path for path, _ in calls] == ["/series", "/markets", "/markets"]


async def test_catalogue_category_exhausts_series_without_synthetic_rows():
    venue, calls = adapter({"/series": {"series": [{"ticker": SERIES}]},
                            "/markets": {"markets": [market("SKIP", market_type="scalar")]}})
    assert await venue.markets(category="Test") == []
    assert len(calls) == 2


@pytest.mark.parametrize("category", ["", "../x", "bad\nvalue"])
async def test_invalid_category_never_reads_series(category):
    venue, calls = adapter({})
    with pytest.raises(VenueError, match="^INVALID_IDENTIFIER$"):
        await venue.markets(category=category)
    assert not calls


@pytest.mark.parametrize("kind", ["same-page", "cross-page", "cross-series", "duplicate-series"])
async def test_catalogue_duplicate_identity_rejected(kind):
    if kind == "same-page":
        responses = {"/markets": {"markets": [market(), market()]}}
    elif kind == "cross-page":
        responses = {"/markets": lambda request: {
            "markets": [market()], "cursor": "" if request.url.params.get("cursor") else "next",
        }}
    else:
        responses = {
            "/series": {"series": [{"ticker": SERIES}, {"ticker": SERIES if kind == "duplicate-series" else "SECOND"}]},
            "/markets": {"markets": [market()]},
        }
    venue, _ = adapter(responses)
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.markets(category="Test" if kind in {"cross-series", "duplicate-series"} else None)


@pytest.mark.parametrize("stage", ["market", "event", "series"])
async def test_market_fee_lookup_rejects_wrong_identity(stage):
    venue, calls = adapter({
        f"/markets/{TICKER}": {"market": market("OTHER" if stage == "market" else TICKER)},
        f"/events/{EVENT}": {"event": {"event_ticker": "OTHER" if stage == "event" else EVENT,
                                     "series_ticker": SERIES}},
        f"/series/{SERIES}": {"series": {"ticker": "OTHER" if stage == "series" else SERIES}},
    })
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.market(TICKER)
    assert len(calls) == {"market": 1, "event": 2, "series": 3}[stage]


@pytest.mark.parametrize("fee_type,multiplier,expected", [
    ("unknown", "1", (None, None)), ("quadratic", None, (None, None)),
    ("quadratic", "0", (Decimal(0), Decimal(0))),
    ("quadratic_with_combo_maker_fees", "1", (Decimal("0.035"), Decimal("0.07"))),
])
async def test_fee_schedule_does_not_invent_unknown_fees(fee_type, multiplier, expected):
    venue, _ = adapter({f"/markets/{TICKER}": {"market": market()},
                        f"/events/{EVENT}": {"event": {"event_ticker": EVENT, "series_ticker": SERIES}},
                        f"/series/{SERIES}": {"series": {"ticker": SERIES, "fee_type": fee_type,
                                                       "fee_multiplier": multiplier}}})
    result = await venue.market(TICKER)
    assert (result["maker_fee_rate"], result["taker_fee_rate"]) == expected


async def test_negative_fee_multiplier_rejected():
    venue, _ = adapter({f"/markets/{TICKER}": {"market": market()},
                        f"/events/{EVENT}": {"event": {"event_ticker": EVENT, "series_ticker": SERIES}},
                        f"/series/{SERIES}": {"series": {"ticker": SERIES, "fee_type": "quadratic",
                                                       "fee_multiplier": "-1"}}})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.market(TICKER)


@pytest.mark.parametrize("kind", ["valid", "foreign-event", "duplicate"])
async def test_related_markets_bound_to_event(kind):
    rows = [market("SKIP", market_type="scalar"), market("PEER", event_ticker="OTHER" if kind == "foreign-event" else EVENT)]
    if kind == "duplicate":
        rows.append(market("PEER"))
    venue, calls = adapter({f"/markets/{TICKER}": {"market": market()}, "/markets": {"markets": rows}})
    if kind == "valid":
        result = await venue.related_markets(TICKER, limit=1)
        assert [row["instrument"] for row in result] == ["PEER"]
    else:
        with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
            await venue.related_markets(TICKER)
    assert calls[1][1]["event_ticker"] == EVENT


def candle(closed=NOW, **overrides):
    return {"end_period_ts": closed, "volume_fp": "2.25",
            "price": {"open_dollars": "0.40", "high_dollars": "0.45",
                      "low_dollars": "0.38", "close_dollars": "0.42"}, **overrides}


def history(rows, ticker=TICKER):
    return {"markets": [{"market_ticker": ticker, "candlesticks": rows}]}


@pytest.mark.parametrize("interval,limit", [(1, 2), (60, 0), (60, 1001), (60.0, 2), (60, True)])
async def test_invalid_history_range_before_io(interval, limit):
    venue, calls = adapter({})
    with pytest.raises(VenueError, match="^INVALID_HISTORY_RANGE$"):
        await venue.candles(TICKER, interval, limit)
    assert not calls


@pytest.mark.parametrize("payload", [
    {"markets": []}, {"markets": [{"market_ticker": "OTHER", "candlesticks": []}]},
    {"markets": [history([])["markets"][0]] * 2},
    history([candle(NOW - 1)]),
    history([candle(), candle()]),
    history([candle(price={}), candle()]),
    history([candle(price={"open_dollars": "1.01", "high_dollars": "1.02",
                          "low_dollars": "0.5", "close_dollars": "0.6"})]),
])
async def test_invalid_history_identity_grid_or_prices(payload):
    venue, _ = adapter({"/markets/candlesticks": payload})
    with pytest.raises(VenueError, match="^INVALID_RESPONSE$"):
        await venue.candles(TICKER, 60, 3)


async def test_history_skips_unobserved_and_outside_window_without_filling_gaps():
    venue, calls = adapter({"/markets/candlesticks": history([
        candle(NOW), candle(NOW - 60, price={}), candle(NOW - 120),
        candle(NOW - 180), candle(NOW + 60),
    ])})
    result = await venue.candles(TICKER, 60, 3)
    assert [row["time"] for row in result] == [NOW - 180, NOW - 60]
    assert all(row["volume"] == Decimal("2.25") for row in result)
    assert calls[0][1]["period_interval"] == "1"


def depth(yes=None, no=None):
    return {"orderbook_fp": {"yes_dollars": [["0.42", "3.25"]] if yes is None else yes,
                             "no_dollars": [["0.56", "2.50"]] if no is None else no}}


@pytest.mark.parametrize("value", [0, 101, True, 1.5])
async def test_invalid_depth_before_io(value):
    venue, calls = adapter({})
    with pytest.raises(VenueError, match="^INVALID_DEPTH$"):
        await venue.orderbook(TICKER, depth=value)
    assert not calls


@pytest.mark.parametrize("meta,error", [
    ({}, "INVALID_RESPONSE"),
    ({"instrument": "OTHER", "source": "KALSHI_PUBLIC", "event_ticker": EVENT}, "INVALID_RESPONSE"),
    ({"instrument": TICKER, "source": "KALSHI_DEMO", "event_ticker": EVENT}, "INVALID_RESPONSE"),
    ({"instrument": TICKER, "source": "KALSHI_PUBLIC", "event_ticker": None}, "INVALID_RESPONSE"),
    ({"instrument": TICKER, "source": "KALSHI_PUBLIC", "event_ticker": "../x"}, "INVALID_IDENTIFIER"),
])
async def test_depth_rejects_incompatible_market_context_before_io(meta, error):
    venue, calls = adapter({})
    with pytest.raises(VenueError, match=f"^{error}$"):
        await venue.orderbook(TICKER, market=meta)
    assert not calls


@pytest.mark.parametrize("public", [False, True])
async def test_orderbook_resolves_metadata_and_preserves_complement(public):
    venue, calls = adapter({f"/markets/{TICKER}": {"market": market()},
                           f"/markets/{TICKER}/orderbook": depth()}, public=public)
    result = await venue.orderbook(TICKER)
    assert result["yes_ask"] == Decimal("0.44")
    assert result["no_ask"] == Decimal("0.58")
    assert result["yes_ask_size"] == Decimal("2.50")
    assert result["event"] == EVENT and result["observed_at"] == NOW
    assert result["source"] == ("KALSHI_PUBLIC" if public else "KALSHI_DEMO")
    assert len(calls) == 2


@pytest.mark.parametrize("yes,no,error", [
    ([["0.42"]], [], "INVALID_RESPONSE"),
    ([["0.42", "0"]], [], "INVALID_RESPONSE"),
    ([["0.42", "1.001"]], [], "INVALID_RESPONSE"),
    ([["1.01", "1"]], [], "INVALID_RESPONSE"),
    ([["0.42", "1"], ["0.42", "2"]], [], "INVALID_RESPONSE"),
    ([["0.60", "1"]], [["0.50", "2"]], "CROSSED_BOOK"),
])
async def test_orderbook_malformed_or_crossed_levels_rejected(yes, no, error):
    venue, calls = adapter({f"/markets/{TICKER}/orderbook": depth(yes, no)})
    meta = {"instrument": TICKER, "event_ticker": EVENT, "source": "KALSHI_PUBLIC"}
    with pytest.raises(VenueError, match=f"^{error}$"):
        await venue.orderbook(TICKER, market=meta)
    assert len(calls) == 1


@pytest.mark.parametrize("yes,no", [([], []), ([["0.4", "1"]], []), ([], [["0.6", "1"]])])
async def test_one_sided_or_empty_book_does_not_invent_liquidity(yes, no):
    venue, _ = adapter({f"/markets/{TICKER}/orderbook": depth(yes, no)})
    result = await venue.orderbook(TICKER, market={
        "instrument": TICKER, "event_ticker": EVENT, "source": "KALSHI_PUBLIC",
    })
    assert (result["yes_bid"] is None) is (not yes)
    assert (result["yes_ask"] is None) is (not no)
    assert result["yes_bid_size"] == Decimal(1 if yes else 0)
    assert result["yes_ask_size"] == Decimal(1 if no else 0)
