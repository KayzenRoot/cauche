"""CD-33: deterministic backtest validation and independent fill-time risk."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.quant.backtest import _run, backtest, parse_time
from app.quant.risk import validate_limits
from app.quant.types import Book, Decision, Market

NOW = 1800000000
STRATEGY = "KALSHI-STRAT-01"


def test_epoch_filter_preserves_integer_timestamp():
    assert parse_time(NOW) == NOW


@pytest.mark.parametrize("value", [True, 1.5, []])
def test_time_filter_rejects_non_epoch_non_string(value):
    with pytest.raises(ValueError, match="Use a UTC epoch timestamp or ISO date/time"):
        parse_time(value)


@pytest.mark.parametrize("value", [
    "2027-01-15T08:00:00Z",
    "2027-01-15T05:00:00-03:00",
])
def test_iso_datetime_normalizes_offset(value):
    assert parse_time(value) == NOW


@pytest.mark.parametrize("value", [
    "2027-01-15T08:00:00",
    "not-a-date",
    "2027-02-30",
])
def test_invalid_or_naive_time_filter(value):
    with pytest.raises(ValueError, match="Invalid UTC date/time filter"):
        parse_time(value)


@pytest.mark.parametrize("end,expected", [(False, 1799971200), (True, 1800057599)])
def test_date_only_filter_has_inclusive_utc_bounds(end, expected):
    assert parse_time("2027-01-15", end=end) == expected


def test_backtest_rejects_exchange_strategy_mismatch():
    with pytest.raises(ValueError, match="Strategy belongs to a different exchange"):
        backtest("DERIV", STRATEGY)


@pytest.mark.parametrize("balance", ["0", "10000001", "1.001"])
def test_backtest_rejects_out_of_range_or_fractional_cent_balance(balance):
    with pytest.raises(ValueError, match="Initial balance must use cents"):
        backtest("KALSHI", STRATEGY, initial_balance=balance)


def test_backtest_rejects_unknown_cost_parameter():
    with pytest.raises(ValueError, match="Unknown simulation cost parameter"):
        backtest("KALSHI", STRATEGY, costs={"unexpected": "0.01"})


@pytest.mark.parametrize("costs", [
    {"slippage": "-0.01"}, {"slippage": "0.51"},
    {"fee_rate": "-0.01"}, {"fee_rate": "0.51"},
])
def test_backtest_rejects_out_of_range_simulation_costs(costs):
    with pytest.raises(ValueError, match="Simulation costs outside supported range"):
        backtest("KALSHI", STRATEGY, costs=costs)


def test_backtest_rejects_inverted_period_before_dataset_access():
    with pytest.raises(ValueError, match="Start date must be before or equal to end date"):
        backtest("KALSHI", STRATEGY, start=NOW + 1, end=NOW)


@pytest.mark.parametrize("widened", [False, True])
def test_fill_rechecks_independent_risk_without_external_execution(widened):
    initial_book = Book(D("0.40"), D("0.44"), D("10"), D("10"),
                        fee_schedule_verified=True)
    first = Market("KALSHI", "FIXTURE-K", "fixture-event", NOW,
                   "SIMULATED_FIXTURE", book=initial_book, closes_at=NOW + 300)
    proposal = Decision(
        STRATEGY, "1", "fixture", source=first.source, timestamp=NOW,
        side="YES", orderable=True, price=D("0.44"), quantity=D("2"),
        exposure=D("0.88"), payout=D("2"),
        instrument=first.instrument, event=first.event,
    )
    # Ask and depth still permit the simulated fill. Only spread changes.
    next_book = replace(initial_book, yes_bid=D("0.19")) if widened else initial_book
    second = replace(first, timestamp=NOW + 15, book=next_book)
    no_signal = replace(proposal, timestamp=second.timestamp, side=None, orderable=False)
    result = _run(
        STRATEGY, {}, ((first, proposal), (second, no_signal)), D("1000"),
        {"slippage": D("0"), "fee_rate": D("0")}, validate_limits(),
    )
    assert result["intent_count"] == 1
    assert result["closed_orders"] == 0
    assert result["trades"] == []
    assert result["metrics"]["closing_balance"] == D("1000")
    assert result["unfilled_intent_at_end"] is False
    if widened:
        assert result["risk_blocks"] == {"SPREAD_LIMIT": 1}
        assert result["open_positions"] == 0
    else:
        assert result["risk_blocks"] == {}
        assert result["open_positions"] == 1
