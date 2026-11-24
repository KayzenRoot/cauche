"""CD-32: deterministic residual simulation and settlement contracts."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.quant.simulation import settle, simulate_fill
from app.quant.types import Book, Candle, Decision, DerivQuote, Market

NOW = 1800000000


def bar(timestamp, close="101"):
    return Candle(timestamp, D("100"), D("103"), D("99"), D(close))


@pytest.fixture
def deriv():
    decision = Decision(
        "DERIV-STRAT-01", "1", "fixture", source="SIMULATED_FIXTURE",
        timestamp=NOW, side="CALL", orderable=True, price=D("5"),
        quantity=D("1"), exposure=D("5"), payout=D("9"),
        duration_seconds=300, instrument="EUR/USD", event="fixture-deriv",
    )
    market = Market(
        "DERIV", decision.instrument, decision.event, NOW + 300,
        decision.source, candles=(bar(NOW + 300),),
        contracts=(DerivQuote("CALL", D("5"), D("9"), 300),),
    )
    return decision, market


@pytest.fixture
def kalshi():
    decision = Decision(
        "KALSHI-STRAT-01", "1", "fixture", source="SIMULATED_FIXTURE",
        timestamp=NOW, side="YES", orderable=True, price=D("0.44"),
        quantity=D("2"), exposure=D("0.88"), payout=D("2"),
        instrument="FIXTURE-K", event="fixture-event",
    )
    market = Market(
        "KALSHI", decision.instrument, decision.event, NOW + 15,
        decision.source,
        book=Book(D("0.40"), D("0.44"), D("10"), D("10"),
                  fee_schedule_verified=True),
        closes_at=NOW + 300,
    )
    return decision, market


@pytest.mark.parametrize("costs", [
    {"slippage": D("-0.01")},
    {"slippage": D("0.51")},
    {"fee_rate": D("-0.01")},
    {"fee_rate": D("0.51")},
])
def test_invalid_costs_raise_before_fill(deriv, costs):
    decision, market = deriv
    with pytest.raises(ValueError, match="Simulation costs outside supported range"):
        simulate_fill(decision, market, **costs)


@pytest.mark.parametrize("case", [
    "missing_quote", "low_payout", "no_candles", "invalid_candle",
])
def test_deriv_requires_matching_quote_and_valid_closed_candle(deriv, case):
    decision, market = deriv
    if case == "missing_quote":
        market = replace(market, contracts=())
    elif case == "low_payout":
        market = replace(market, contracts=(replace(market.contracts[0], payout=D("8")),))
    elif case == "no_candles":
        market = replace(market, candles=())
    else:
        market = replace(market, candles=(replace(market.candles[0], close=D("0")),))
    assert simulate_fill(decision, market) is None


@pytest.mark.parametrize("case", ["same_observation", "stale_entry"])
def test_deriv_entry_requires_new_and_timely_candle(deriv, case):
    decision, market = deriv
    if case == "same_observation":
        market = replace(market, candles=(bar(NOW),))
    else:
        market = replace(market, timestamp=NOW + 316)
    assert simulate_fill(decision, market) is None


def test_deriv_duration_requires_granularity_alignment(deriv):
    decision, market = deriv
    decision = replace(decision, duration_seconds=301)
    market = replace(market, contracts=(replace(market.contracts[0], duration_seconds=301),))
    assert simulate_fill(decision, market) is None


def test_deriv_expiry_cannot_exceed_market_close(deriv):
    decision, market = deriv
    assert simulate_fill(decision, replace(market, closes_at=NOW + 599)) is None


@pytest.mark.parametrize("case", ["missing_book", "crossed_book", "unsupported_exchange"])
def test_binary_fill_requires_supported_exchange_and_valid_book(kalshi, case):
    decision, market = kalshi
    if case == "missing_book":
        market = replace(market, book=None)
    elif case == "crossed_book":
        market = replace(market, book=replace(market.book, yes_bid=D("0.45")))
    else:
        market = replace(market, exchange="UNSUPPORTED")
    assert simulate_fill(decision, market) is None


@pytest.mark.parametrize("case", ["unverified_fee", "invalid_side"])
def test_binary_fill_requires_verified_fee_schedule_and_side(kalshi, case):
    decision, market = kalshi
    if case == "unverified_fee":
        market = replace(market, book=replace(market.book, fee_schedule_verified=False))
    else:
        decision = replace(decision, side="CALL")
    assert simulate_fill(decision, market) is None


@pytest.mark.parametrize("field,value", [
    ("exchange", "DERIV"), ("instrument", "DIFFERENT-CONTRACT"),
])
def test_settlement_rejects_different_contract(kalshi, field, value):
    decision, market = kalshi
    fill = simulate_fill(decision, market)
    assert fill is not None
    closing = replace(market, timestamp=NOW + 300, outcome=True, **{field: value})
    assert settle(fill, closing) is None


def test_settlement_rejects_observation_before_fill(kalshi):
    decision, market = kalshi
    fill = simulate_fill(decision, market)
    assert fill is not None
    assert settle(fill, replace(market, timestamp=NOW + 14, outcome=True)) is None


def test_binary_settlement_rejects_outcome_before_expiry(kalshi):
    decision, market = kalshi
    fill = simulate_fill(decision, market)
    assert fill is not None
    assert settle(fill, replace(market, timestamp=NOW + 299, outcome=True)) is None


@pytest.mark.parametrize("case", ["missing", "invalid", "unclosed", "duplicate"])
def test_deriv_settlement_requires_unique_valid_closed_expiry_candle(deriv, case):
    decision, market = deriv
    fill = simulate_fill(decision, market)
    assert fill is not None
    expiry = bar(NOW + 600, "102")
    if case == "missing":
        candles = (bar(NOW + 599),)
    elif case == "invalid":
        candles = (replace(expiry, close=D("0")),)
    elif case == "unclosed":
        candles = (replace(expiry, closed=False),)
    else:
        candles = (expiry, expiry)
    assert settle(fill, replace(market, timestamp=NOW + 600, candles=candles)) is None


def test_deriv_exact_fee_and_settlement_positive_control(deriv):
    decision, market = deriv
    fill = simulate_fill(decision, market, fee_rate=D("0.01"))
    assert fill is not None
    assert fill.fee == D("0.05")
    assert fill.exposure == D("5")
    assert fill.expires_at == NOW + 600
    closing = replace(market, timestamp=NOW + 600, candles=(bar(NOW + 600, "102"),))
    assert settle(fill, closing) == D("3.95")


def test_binary_exact_fee_and_settlement_positive_control(kalshi):
    decision, market = kalshi
    fill = simulate_fill(decision, market)
    assert fill is not None
    assert fill.quantity == D("2")
    assert fill.exposure == D("0.88")
    assert fill.fee == D("0.0345")
    closing = replace(market, timestamp=NOW + 300, outcome=True)
    assert settle(fill, closing) == D("1.0855")
