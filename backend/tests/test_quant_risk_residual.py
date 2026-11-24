"""CD-29: independent risk gate, deterministic local inputs only."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.quant.risk import RiskState, check_risk, validate_limits
from app.quant.types import Book, Decision, DerivQuote, Market

D = Decimal
NOW = 1800000000


@pytest.fixture
def deriv():
    market = Market("DERIV", "FIXTURE-D", "FIXTURE-E", NOW, "SIMULATED_FIXTURE",
                    contracts=(DerivQuote("CALL", D(5), D(9), 300),))
    decision = Decision("fixture-strategy", "fixture-version", "fixture",
                        source=market.source, timestamp=NOW, side="CALL",
                        orderable=True, price=D(5), quantity=D(1), exposure=D(5),
                        payout=D(9), duration_seconds=300,
                        instrument=market.instrument, event=market.event)
    return decision, market


@pytest.fixture
def kalshi():
    market = Market("KALSHI", "FIXTURE-K", "FIXTURE-E", NOW, "SIMULATED_FIXTURE",
                    book=Book(D("0.40"), D("0.44"), D(10), D(10),
                              fee_schedule_verified=True))
    decision = Decision("fixture-strategy", "fixture-version", "fixture",
                        source=market.source, timestamp=NOW, side="YES",
                        orderable=True, price=D("0.42"), quantity=D(2),
                        exposure=D("0.84"), payout=D(2),
                        instrument=market.instrument, event=market.event)
    return decision, market


def assert_blocked(decision, market, code, **kwargs):
    result = check_risk(decision, market, **kwargs)
    assert result.allowed is False
    assert result.code == code
    assert result.at == kwargs.get("now", market.timestamp)


@pytest.mark.parametrize("limits,message", [
    ({"unknown_limit": 1}, "Unknown risk limit"),
    ({"kill_switch": "false"}, "kill_switch must be a boolean"),
])
def test_invalid_risk_configuration_fails_closed(deriv, limits, message):
    with pytest.raises(ValueError, match=message):
        validate_limits(limits)
    assert_blocked(*deriv, "RISK_CONFIGURATION_INVALID", limits=limits)


@pytest.mark.parametrize("age", [0, 1])
def test_market_close_boundary_denies_order(deriv, age):
    decision, market = deriv
    assert_blocked(decision, replace(market, closes_at=NOW), "MARKET_CLOSED",
                   now=NOW + age)


@pytest.mark.parametrize("state_changes,decision_changes", [
    ({"global_exposure": D("-1")}, {}),
    ({"daily_loss": D("NaN")}, {}),
    ({}, {"estimated_costs": D("Infinity")}),
    ({}, {"quantity": D(0)}),
    ({}, {"payout": D(0)}),
])
def test_nonfinite_negative_or_zero_money_denied(deriv, state_changes, decision_changes):
    decision, market = deriv
    assert_blocked(replace(decision, **decision_changes), market, "RISK_INPUT_INVALID",
                   state=RiskState(**state_changes))


@pytest.mark.parametrize("changes", [
    {"side": "YES"}, {"quantity": D(2)}, {"price": D(4)}, {"duration_seconds": 0},
])
def test_deriv_incompatible_product_input_denied(deriv, changes):
    decision, market = deriv
    assert_blocked(replace(decision, **changes), market, "PRODUCT_INPUT_INVALID")


@pytest.mark.parametrize("changes", [
    {"side": "CALL"}, {"price": D(1)}, {"exposure": D("0.85")},
])
def test_kalshi_incompatible_product_input_denied(kalshi, changes):
    decision, market = kalshi
    assert_blocked(replace(decision, **changes), market, "PRODUCT_INPUT_INVALID")


@pytest.mark.parametrize("kind", ["missing", "unverified", "crossed"])
def test_kalshi_book_and_fee_provenance_required(kalshi, kind):
    decision, market = kalshi
    book = market.book
    if kind == "missing":
        book = None
    elif kind == "unverified":
        book = replace(book, fee_schedule_verified=False)
    else:
        book = replace(book, yes_bid=D("0.45"))
    assert_blocked(decision, replace(market, book=book), "BOOK_OR_FEE_SCHEDULE_INVALID")


@pytest.mark.parametrize("price,quantity", [(D("0.42001"), D(2)), (D("0.42"), D("2.001"))])
def test_kalshi_invalid_fixed_point_precision_denied(kalshi, price, quantity):
    decision, market = kalshi
    assert_blocked(replace(decision, price=price, quantity=quantity,
                           exposure=price * quantity), market, "FIXED_POINT_PRECISION")


def test_unsupported_product_denied(deriv):
    decision, market = deriv
    assert_blocked(decision, replace(market, exchange="FIXTURE_UNSUPPORTED"),
                   "UNSUPPORTED_PRODUCT")


def test_demo_label_without_balance_denied_offline(kalshi):
    decision, market = kalshi
    # Synthetic observation carrying the expected demo source label. No venue I/O.
    source = "KALSHI_DEMO"
    assert_blocked(replace(decision, source=source), replace(market, source=source),
                   "BALANCE_UNAVAILABLE", state=RiskState(mode="BROKER_DEMO"))


@pytest.mark.parametrize("fixture_name", ["deriv", "kalshi"])
def test_valid_fixture_is_allowed_without_external_io(request, fixture_name):
    decision, market = request.getfixturevalue(fixture_name)
    result = check_risk(decision, market)
    assert result.allowed is True and result.code == "ALLOWED"
    assert result.at == NOW
