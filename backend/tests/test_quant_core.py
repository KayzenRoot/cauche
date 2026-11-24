"""Regression tests for quantitative contracts and simulation provenance."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain import (
    Candle,
    Decision,
    RiskState,
    SimFill,
    adx,
    atr,
    backtest,
    bollinger,
    catalog,
    check_risk,
    dataset,
    donchian,
    ema,
    evaluate,
    event_fee,
    rsi,
    settle,
    simulate_fill,
    validate_limits,
    validate_parameters,
)

D = Decimal
EUR = "EUR/USD"


@pytest.mark.parametrize(
    ("strategy", "exchange", "index", "side", "status"),
    [
        ("DERIV-STRAT-01", "DERIV", 193, "CALL", "ORDERABLE"),
        ("DERIV-STRAT-02", "DERIV", 255, "CALL", "ORDERABLE"),
        ("DERIV-STRAT-03", "DERIV", 308, "CALL", "ORDERABLE"),
        ("KALSHI-STRAT-01", "KALSHI", 0, "YES", "ORDERABLE"),
        ("KALSHI-STRAT-02", "KALSHI", 0, "YES", "ORDERABLE"),
        ("KALSHI-STRAT-03", "KALSHI", 0, "NO+YES", "ADVISORY"),
    ],
)
def test_all_strategies_emit_deterministic_positive_and_negative_decisions(
    strategy,
    exchange,
    index,
    side,
    status,
):
    market = dataset(exchange)[index]
    decision = evaluate(strategy, market)
    assert decision.side == side
    assert decision.status == status
    assert decision.as_dict() == evaluate(strategy, market).as_dict()
    assert Decision.from_dict(decision.as_dict()) == decision
    invalid = replace(market, candles=(), book=None)
    assert evaluate(strategy, invalid).status == "NO_SIGNAL"


def test_exact_indicators_and_warmup():
    rising = [D(value) for value in range(1, 45)]
    bars = [
        Candle(index, price, price + 1, price - D("0.1"), price)
        for index, price in enumerate(rising)
    ]
    assert ema([D(1), D(2), D(3), D(4)], 3) == D(3)
    assert rsi(rising, 14) == D(100)
    assert rsi([D(1)] * 15, 14) == D(50)
    assert adx(bars, 14) == D(100)
    assert atr(bars, 14) == D(2)
    assert donchian(bars, 3) == (D("41.9"), D(45))
    assert bollinger([D(1), D(3)], 2) == (D(0), D(2), D(4))
    assert all(
        value is None
        for value in (
            ema([], 2),
            rsi([], 2),
            adx([], 2),
            atr([], 2),
            bollinger([], 2),
            donchian([], 2),
        )
    )


def test_decisions_exclude_incomplete_and_future_candles():
    fixture = dataset("DERIV", EUR)
    series = fixture[255]
    future = fixture[256].candles[-1]
    future_confirm = fixture[257].confirmation[-1]
    bad = Candle(series.timestamp, D(4), D(1), D(2), D(4), closed=False)
    extended = replace(
        series,
        candles=series.candles + (future, bad),
        confirmation=series.confirmation + (future_confirm,),
    )
    strategy = "DERIV-STRAT-02"
    assert evaluate(strategy, extended) == evaluate(strategy, series)
    unsorted = replace(series, candles=series.candles[::-1])
    assert evaluate(strategy, unsorted).reason == "CANDLE_DATA_INVALID"


@pytest.mark.parametrize(
    ("strategy", "parameters"),
    [
        ("DERIV-STRAT-01", {"ema_fast": 30, "ema_slow": 25}),
        ("DERIV-STRAT-01", {"stake": "0.01"}),
        ("DERIV-STRAT-01", {"stake": "5.0001"}),
        ("DERIV-STRAT-01", {"ema_fast": 20.5}),
        ("KALSHI-STRAT-01", {"quantity": "2.123"}),
        ("KALSHI-STRAT-01", {"quantity": "NaN"}),
        ("KALSHI-STRAT-01", {"slippage": True}),
        ("KALSHI-STRAT-01", {"lookahead": True}),
    ],
)
def test_parameter_validation(strategy, parameters):
    with pytest.raises(ValueError):
        validate_parameters(strategy, parameters)


def test_unverified_and_different_settlement_inputs_fail_closed():
    first = dataset("KALSHI")[0]
    assert (
        evaluate(
            "KALSHI-STRAT-01",
            replace(
                first,
                forecast=replace(first.forecast, issued_at=first.timestamp + 60),
            ),
        ).reason
        == "INDEPENDENT_FORECAST_UNAVAILABLE"
    )
    assert (
        evaluate(
            "KALSHI-STRAT-01",
            replace(
                first,
                source="KALSHI_OFFICIAL_PUBLIC",
            ),
        ).reason
        == "INDEPENDENT_FORECAST_UNAVAILABLE"
    )
    assert (
        evaluate(
            "KALSHI-STRAT-03",
            replace(
                first,
                relation=replace(first.relation, rules_id="wrong_date_or_source"),
            ),
        ).reason
        == "RELATED_MARKET_RULES_OR_FRESHNESS_INVALID"
    )
    assert (
        evaluate("KALSHI-STRAT-02", replace(first, adverse=True)).reason
        == "ADVERSE_SELECTION"
    )
    assert (
        evaluate(
            "KALSHI-STRAT-02",
            replace(
                first,
                book=replace(first.book, fee_schedule_verified=False),
            ),
        ).reason
        == "FEE_SCHEDULE_UNVERIFIED"
    )


@pytest.mark.parametrize(
    ("state", "limits", "age", "expected"),
    [
        (RiskState(), {}, 0, "ALLOWED"),
        (RiskState(mode="REAL"), {}, 0, "ENVIRONMENT_DENIED"),
        (RiskState(mode="BROKER_DEMO"), {}, 0, "SIMULATED_BROKER_INPUT"),
        (RiskState(), {"kill_switch": True}, 0, "KILL_SWITCH"),
        (RiskState(), {"max_order_exposure": "99999"}, 0, "RISK_CONFIGURATION_INVALID"),
        (RiskState(unknown_order=True), {}, 0, "AMBIGUOUS_ORDER_STATE"),
        (RiskState(balance_discrepancy=True), {}, 0, "BALANCE_DIVERGENCE"),
        (RiskState(errors=3), {}, 0, "ERROR_LIMIT"),
        (RiskState(), {}, 46, "STALE_OR_FUTURE_DATA"),
        (RiskState(), {}, -1, "STALE_OR_FUTURE_DATA"),
        (RiskState(), {"max_order_exposure": "3"}, 0, "ORDER_EXPOSURE_LIMIT"),
        (RiskState(strategy_exposure=D(21)), {}, 0, "STRATEGY_EXPOSURE_LIMIT"),
        (RiskState(exchange_exposure=D(31)), {}, 0, "EXCHANGE_EXPOSURE_LIMIT"),
        (RiskState(global_exposure=D(46)), {}, 0, "GLOBAL_EXPOSURE_LIMIT"),
        (RiskState(market_exposure=D(16)), {}, 0, "MARKET_EXPOSURE_LIMIT"),
        (RiskState(event_exposure=D(21)), {}, 0, "EVENT_EXPOSURE_LIMIT"),
        (RiskState(daily_loss=D(20)), {}, 0, "DAILY_LOSS_LIMIT"),
        (RiskState(drawdown=D(50)), {}, 0, "DRAWDOWN_LIMIT"),
        (RiskState(open_orders=5), {}, 0, "OPEN_ORDERS_LIMIT"),
        (RiskState(orders_last_minute=3), {}, 0, "ORDER_RATE_LIMIT"),
        (RiskState(available_balance=D(4)), {}, 0, "INSUFFICIENT_BALANCE"),
        (RiskState(), {"min_payout_ratio": "2.00"}, 0, "PAYOUT_LIMIT"),
    ],
)
def test_risk_limits(state, limits, age, expected):
    market = dataset("DERIV")[193]
    decision = evaluate("DERIV-STRAT-01", market)
    result = check_risk(decision, market, limits, state, now=market.timestamp + age)
    assert result.code == expected
    assert result.allowed == (expected == "ALLOWED")


def test_cooldown_applies_strategy_floor():
    market = dataset("DERIV")[255]
    decision = evaluate("DERIV-STRAT-02", market)
    state = RiskState(last_order_at=market.timestamp - 600)
    assert check_risk(decision, market, state=state).code == "COOLDOWN"


def test_independent_risk_denies_untrusted_quote_and_intent_provenance():
    market = dataset("DERIV")[193]
    decision = evaluate("DERIV-STRAT-01", market)
    tampered = replace(decision, payout=D("10.00"))
    assert check_risk(tampered, market).code == "QUOTE_UNAVAILABLE"
    assert check_risk(decision, replace(market, event="OTHER_EVENT")).code == (
        "INTENT_PROVENANCE_MISMATCH"
    )


def test_spread_depth_and_non_atomic_advisory_are_independent_risk_outcomes():
    market = dataset("KALSHI")[0]
    decision = evaluate("KALSHI-STRAT-01", market)
    assert check_risk(decision, market, {"max_spread": "0.03"}).code == "SPREAD_LIMIT"
    assert check_risk(decision, market, {"min_depth": "20"}).code == "LIQUIDITY_LIMIT"
    advisory = evaluate("KALSHI-STRAT-03", market)
    assert check_risk(advisory, market).code == "INTENT_NOT_ORDERABLE"
    assert simulate_fill(advisory, market) is None


def test_risk_reserves_kalshi_fees_even_when_strategy_omits_them():
    market = dataset("KALSHI")[0]
    decision = replace(evaluate("KALSHI-STRAT-01", market), estimated_costs=D(0))
    state = RiskState(available_balance=decision.exposure)
    assert check_risk(decision, market, state=state).code == "INSUFFICIENT_BALANCE"


@pytest.mark.parametrize("duration", [300, 600, 900])
def test_deriv_expiration_and_stake_are_product_specific(duration):
    observations = dataset("DERIV")
    decision = evaluate(
        "DERIV-STRAT-01", observations[193], {"duration_seconds": duration}
    )
    fill = simulate_fill(decision, observations[194], fee_rate=D("0.01"))
    assert fill.exposure == D(5) and fill.quantity == 1 and fill.payout == D("9.30")
    assert fill.fee == D("0.05")
    assert settle(fill, observations[194]) is None
    closing = observations[194 + duration // 300]
    net_pnl = settle(fill, closing)
    assert net_pnl in {D("4.25"), D("-5.05")}
    assert SimFill.from_dict(fill.as_dict()) == fill
    tie = Candle(
        fill.expires_at,
        fill.entry_price,
        fill.entry_price,
        fill.entry_price,
        fill.entry_price,
    )
    tied = replace(closing, candles=closing.candles[:-1] + (tie,))
    assert settle(fill, tied) == D("-5.05")


def test_fixture_quote_rescales_stake_but_live_quote_must_match_exact_stake():
    market = dataset("DERIV")[193]
    decision = evaluate("DERIV-STRAT-01", market, {"stake": "2.00"})
    assert decision.exposure == D("2.00")
    assert decision.payout == D("3.72")
    official = replace(market, source="DERIV_OFFICIAL_PUBLIC")
    assert (
        evaluate(
            "DERIV-STRAT-01",
            official,
            {"stake": "2.00"},
        ).reason
        == "MATCHING_CONTRACT_UNAVAILABLE"
    )


def test_fixed_point_fee_rounds_trade_fee_and_balance_at_distinct_precisions():
    price, quantity, rate = D("0.0550"), D("1.00"), D("0.07")
    assert event_fee(price, quantity, rate, balance_precision=D("0.01")) == D(
        "0.005000"
    )
    assert event_fee(price, quantity, rate) == D("0.003700")
    assert event_fee(D("0.0048"), D("0.01"), D(0)) == D("0.000052")


def test_kalshi_maker_latency_partial_fill_and_settlement():
    frames = dataset("KALSHI")
    decision = evaluate("KALSHI-STRAT-02", frames[0])
    assert decision.post_only and decision.price == D("0.45")
    assert simulate_fill(decision, frames[0]) is None
    assert (
        simulate_fill(decision, replace(frames[1], observed_sell_volume=D(0))) is None
    )
    assert (
        simulate_fill(decision, replace(frames[1], source="KALSHI_OFFICIAL_PUBLIC"))
        is None
    )
    assert simulate_fill(decision, replace(frames[1], event="OTHER_EVENT")) is None
    fill = simulate_fill(decision, frames[1])
    assert fill.quantity == D("2.00")
    assert fill.exposure == D("0.90")
    assert fill.fee == D("0.0087")
    assert settle(fill, frames[2]) is None
    assert settle(fill, frames[12]) == D("1.0913")
    assert settle(fill, replace(frames[12], event="OTHER_EVENT")) is None


@pytest.mark.parametrize(
    ("exchange", "strategy", "expected_trades"),
    [
        ("DERIV", "DERIV-STRAT-01", 40),
        ("DERIV", "DERIV-STRAT-02", 6),
        ("DERIV", "DERIV-STRAT-03", 5),
        ("KALSHI", "KALSHI-STRAT-01", 30),
        ("KALSHI", "KALSHI-STRAT-02", 6),
        ("KALSHI", "KALSHI-STRAT-03", 0),
    ],
)
def test_complete_backtest_matrix(exchange, strategy, expected_trades):
    actual = backtest(exchange, strategy)
    metrics = actual["metrics"]
    assert actual["source"] == "SIMULATED_FIXTURE"
    assert actual["mode"] == "REPLAY"
    assert metrics["trade_count"] == expected_trades
    assert D(metrics["net_pnl"]) + D(metrics["initial_balance"]) == D(
        metrics["closing_balance"]
    )
    assert sum((D(trade["fee"]) for trade in actual["trades"]), D(0)) == D(
        metrics["fees"]
    )
    if strategy == "KALSHI-STRAT-01":
        assert metrics["scored_forecasts"] == 6
        assert D(0) < D(metrics["brier_score"]) < D(1)
    if strategy == "KALSHI-STRAT-03":
        assert actual["decision_counts"]["ADVISORY"] > 0
        assert actual["intent_count"] == 0


def test_time_splits_costs_and_reproducibility():
    strategy = "DERIV-STRAT-02"
    basic = backtest("DERIV", strategy)
    extra = backtest("DERIV", strategy, costs={"fee_rate": "0.01"})
    assert extra == backtest("DERIV", strategy, costs={"fee_rate": "0.01"})
    assert D(extra["metrics"]["fees"]) > D(basic["metrics"]["fees"])
    assert D(extra["metrics"]["net_pnl"]) < D(basic["metrics"]["net_pnl"])
    parts = basic["temporal_partitions"]
    assert parts["train"]["end"] < parts["validation"]["start"] < parts["test"]["start"]
    assert basic["out_of_sample"] == parts["test"]
    assert basic["slippage_sensitivity"]["metrics"] != basic["metrics"]
    with pytest.raises(ValueError):
        backtest("DERIV", strategy, start="2001-01-01", end="2001-01-02")


def test_non_overlapping_history_and_public_catalog():
    for venue in ("DERIV", "KALSHI"):
        frames = dataset(venue)
        timestamps = [item.timestamp for item in frames]
        assert timestamps == sorted(set(timestamps))
        assert all(item.simulated for item in frames)
    assert len(catalog()) == 6
    with pytest.raises(ValueError):
        validate_limits({"kill_switch": "true"})
