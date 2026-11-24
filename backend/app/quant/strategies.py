"""Six bounded, configurable decisions over normalized observations."""

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from .indicators import adx, atr, bollinger, closed_bars, donchian, ema, rsi
from .types import (
    D,
    FOUR_PLACES,
    ONE,
    ZERO,
    Decision,
    Estimator,
    Market,
    decimal,
    event_fee,
)

VERSION = "1.0.0"


@dataclass(frozen=True)
class Parameter:
    default: str | int
    minimum: str | int
    maximum: str | int

    def validate(self, value: Any) -> int | str:
        number = decimal(value)
        if not decimal(self.minimum) <= number <= decimal(self.maximum):
            raise ValueError("Value outside supported range")
        if isinstance(self.default, int):
            if number != number.to_integral_value():
                raise ValueError("Expected an integer")
            return int(number)
        if number.quantize(FOUR_PLACES) != number:
            raise ValueError("Use no more than four decimal places")
        return str(number)


DERIV_COMMON = {
    "stake": Parameter("5.00", "1", "10"),
    "duration_seconds": Parameter(300, 60, 3600),
    "min_payout_ratio": Parameter("1.55", "1.40", "3.00"),
}
KALSHI_COMMON = {
    "quantity": Parameter("5.00", "1", "10"),
    "min_depth": Parameter("3", "1", "100"),
    "slippage": Parameter("0.01", "0", "0.10"),
}
STRATEGIES = {
    "DERIV-STRAT-01": (
        "Trend Following + Pullback",
        {
            **DERIV_COMMON,
            "ema_fast": Parameter(20, 5, 30),
            "ema_slow": Parameter(50, 25, 100),
            "rsi_period": Parameter(14, 5, 30),
            "adx_period": Parameter(14, 5, 30),
            "adx_min": Parameter("20", "10", "50"),
            "pullback_atr": Parameter("1.25", "0.1", "3"),
            "atr_floor": Parameter("0.0001", "0.0001", "0.02"),
            "atr_ceiling": Parameter("0.006", "0.0002", "0.05"),
        },
    ),
    "DERIV-STRAT-02": (
        "Donchian Breakout + ATR",
        {
            **DERIV_COMMON,
            "lookback": Parameter(20, 5, 60),
            "atr_period": Parameter(14, 5, 30),
            "consolidation_atr": Parameter("8", "2", "20"),
            "minimum_breakout_atr": Parameter("0.15", "0.05", "1"),
            "maximum_breakout_atr": Parameter("1.50", "0.2", "4"),
            "atr_floor": Parameter("0.0001", "0.0001", "0.02"),
            "atr_ceiling": Parameter("0.006", "0.0002", "0.05"),
            "cooldown_seconds": Parameter(900, 300, 7200),
        },
    ),
    "DERIV-STRAT-03": (
        "Bollinger + RSI Mean Reversion",
        {
            **DERIV_COMMON,
            "lookback": Parameter(20, 5, 60),
            "width": Parameter("2", "1", "4"),
            "rsi_period": Parameter(14, 5, 30),
            "adx_period": Parameter(14, 5, 30),
            "adx_max": Parameter("24", "10", "35"),
            "rsi_low": Parameter("38", "15", "45"),
        },
    ),
    "KALSHI-STRAT-01": (
        "AI Probability Edge",
        {
            **KALSHI_COMMON,
            "min_edge": Parameter("0.04", "0.01", "0.30"),
            "max_uncertainty": Parameter("0.15", "0.02", "0.35"),
        },
    ),
    "KALSHI-STRAT-02": (
        "Market Making",
        {
            **KALSHI_COMMON,
            "min_spread": Parameter("0.05", "0.02", "0.20"),
            "inventory_limit": Parameter("10", "2", "20"),
            "min_net_spread": Parameter("0.01", "0.005", "0.20"),
            "max_imbalance": Parameter("3", "1", "10"),
        },
    ),
    "KALSHI-STRAT-03": (
        "Logical Arbitrage Scanner",
        {
            **KALSHI_COMMON,
            "min_edge": Parameter("0.02", "0.01", "0.30"),
            "max_leg_age_seconds": Parameter(30, 1, 60),
        },
    ),
}


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": strategy,
            "exchange": strategy.split("-", 1)[0],
            "name": name,
            "version": VERSION,
            "defaults": {key: item.default for key, item in schema.items()},
            "parameter_schema": {
                key: {
                    "type": "integer" if isinstance(item.default, int) else "decimal",
                    "min": item.minimum,
                    "max": item.maximum,
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                }
                for key, item in schema.items()
            },
        }
        for strategy, (name, schema) in STRATEGIES.items()
    ]


def validate_parameters(
    strategy: str, parameters: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy")
    schema = STRATEGIES[strategy][1]
    supplied = {} if parameters is None else dict(parameters)
    if supplied.keys() - schema.keys():
        raise ValueError("Unknown strategy parameter")
    config = {}
    for key, specification in schema.items():
        try:
            config[key] = specification.validate(
                supplied.get(key, specification.default)
            )
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from error
    if config.get("ema_fast", 0) >= config.get("ema_slow", 1):
        raise ValueError("Fast EMA must be shorter than slow EMA")
    if decimal(config.get("atr_floor", 0)) >= decimal(config.get("atr_ceiling", 1)):
        raise ValueError("ATR floor must be below ceiling")
    if decimal(config.get("minimum_breakout_atr", 0)) >= decimal(
        config.get("maximum_breakout_atr", 1)
    ):
        raise ValueError("Minimum breakout distance must be below maximum")
    if "quantity" in config and (decimal(config["quantity"]) * 100) % ONE:
        raise ValueError("Quantity must align to fixed-point hundredths")
    if "stake" in config and (decimal(config["stake"]) * 100) % ONE:
        raise ValueError("Stake must align to cents")
    return config


def trend(market: Market, bars: tuple, p: dict) -> tuple[str | None, str]:
    confirmation = closed_bars(market.confirmation, market.timestamp, 900)
    if confirmation is None:
        return None, "CONFIRMATION_DATA_INVALID"
    values = [item.close for item in bars]
    history = [item.close for item in confirmation]
    fast = ema(values, p["ema_fast"])
    slow = ema(values, p["ema_slow"])
    prior_fast = ema(values[:-1], p["ema_fast"])
    confirmation_fast = ema(history, p["ema_fast"])
    confirmation_slow = ema(history, p["ema_slow"])
    momentum = rsi(values, p["rsi_period"])
    prior_momentum = rsi(values[:-1], p["rsi_period"])
    strength = adx(bars, p["adx_period"])
    volatility = atr(bars)
    indicators = (
        fast,
        slow,
        prior_fast,
        confirmation_fast,
        confirmation_slow,
        momentum,
        prior_momentum,
        strength,
        volatility,
    )
    if any(indicator is None for indicator in indicators):
        return None, "WARM_UP"
    if strength < D(p["adx_min"]):
        return None, "TREND_WEAK"
    if not D(p["atr_floor"]) <= volatility / values[-1] <= D(p["atr_ceiling"]):
        return None, "VOLATILITY_OUTSIDE_RANGE"
    tolerance = volatility * D(p["pullback_atr"])
    if fast > slow and confirmation_fast > confirmation_slow:
        near_average = abs(bars[-2].low - prior_fast) <= tolerance
        if near_average and bars[-1].close > fast and D(42) <= momentum < D(75):
            if momentum > prior_momentum and bars[-1].close > bars[-2].close:
                return "CALL", "UPTREND_PULLBACK_CONFIRMED"
    if fast < slow and confirmation_fast < confirmation_slow:
        near_average = abs(bars[-2].high - prior_fast) <= tolerance
        if near_average and bars[-1].close < fast and D(25) < momentum <= D(58):
            if momentum < prior_momentum and bars[-1].close < bars[-2].close:
                return "PUT", "DOWNTREND_PULLBACK_CONFIRMED"
    return None, "PULLBACK_NOT_CONFIRMED"


def breakout(market: Market, bars: tuple, p: dict) -> tuple[str | None, str]:
    channel = donchian(bars[:-1], p["lookback"])
    volatility = atr(bars[:-1], p["atr_period"])
    if channel is None or volatility is None:
        return None, "WARM_UP"
    last = bars[-1]
    if not D(p["atr_floor"]) <= volatility / last.close <= D(p["atr_ceiling"]):
        return None, "VOLATILITY_OUTSIDE_RANGE"
    lower, upper = channel
    if upper - lower > volatility * D(p["consolidation_atr"]):
        return None, "NO_CONSOLIDATION"
    minimum = volatility * D(p["minimum_breakout_atr"])
    maximum = volatility * D(p["maximum_breakout_atr"])
    body = last.close - last.open
    if minimum <= last.close - upper <= maximum and body >= minimum:
        return "CALL", "UPPER_CHANNEL_BREAK_CONFIRMED"
    if minimum <= lower - last.close <= maximum and -body >= minimum:
        return "PUT", "LOWER_CHANNEL_BREAK_CONFIRMED"
    return None, "NO_CONFIRMED_BREAK"


def mean_reversion(market: Market, bars: tuple, p: dict) -> tuple[str | None, str]:
    values = [item.close for item in bars]
    band = bollinger(values[:-2], p["lookback"], D(p["width"]))
    momentum = rsi(values, p["rsi_period"])
    prior_momentum = rsi(values[:-1], p["rsi_period"])
    strength = adx(bars[:-1], p["adx_period"])
    if any(value is None for value in (band, momentum, prior_momentum, strength)):
        return None, "WARM_UP"
    if strength > D(p["adx_max"]):
        return None, "TREND_TOO_STRONG"
    lower, middle, upper = band
    if lower == upper:
        return None, "NO_PRICE_VARIATION"
    low_rsi = D(p["rsi_low"])
    high_rsi = 100 - low_rsi
    if (
        values[-2] < lower < values[-1] <= middle
        and prior_momentum <= low_rsi < momentum
    ):
        return "CALL", "LOWER_BAND_RECOVERY"
    if (
        values[-2] > upper > values[-1] >= middle
        and prior_momentum >= high_rsi > momentum
    ):
        return "PUT", "UPPER_BAND_REJECTION"
    return None, "NO_BAND_RECOVERY"


DerivDecision = Callable[[Market, tuple, dict], tuple[str | None, str]]
DERIV_RULES: dict[str, DerivDecision] = {
    "DERIV-STRAT-01": trend,
    "DERIV-STRAT-02": breakout,
    "DERIV-STRAT-03": mean_reversion,
}


def deriv_decision(
    strategy: str, market: Market, p: dict, result: Callable
) -> Decision:
    if market.granularity != 300:
        return result("UNSUPPORTED_ENTRY_TIMEFRAME")
    bars = closed_bars(market.candles, market.timestamp, market.granularity)
    if bars is None:
        return result("CANDLE_DATA_INVALID")
    if len(bars) < 2:
        return result("WARM_UP")
    side, reason = DERIV_RULES[strategy](market, bars, p)
    if side is None:
        return result(reason)
    quote = market.deriv_quote(side, D(p["stake"]), p["duration_seconds"])
    if quote is None:
        return result("MATCHING_CONTRACT_UNAVAILABLE", risks=(reason,))
    if quote.payout / quote.stake < D(p["min_payout_ratio"]):
        return result("PAYOUT_BELOW_THRESHOLD", risks=(reason,))
    return result(
        reason,
        side=side,
        orderable=True,
        price=quote.stake,
        quantity=ONE,
        exposure=quote.stake,
        payout=quote.payout,
        duration_seconds=quote.duration_seconds,
        risks=("BINARY_MAX_LOSS_STAKE", "OUTCOME_PROBABILITY_UNCALIBRATED"),
        cooldown_seconds=p.get("cooldown_seconds", 0),
    )


def probability_edge(market: Market, p: dict, result: Callable) -> Decision:
    book, forecast = market.book, market.forecast
    if forecast is None or not forecast.is_usable(market):
        return result("INDEPENDENT_FORECAST_UNAVAILABLE")
    forecast_fields = {
        "forecast": forecast,
        "probability": forecast.probability,
        "uncertainty": forecast.uncertainty,
    }
    if forecast.uncertainty > D(p["max_uncertainty"]):
        return result("FORECAST_TOO_UNCERTAIN", **forecast_fields)
    candidates = []
    for side, chance in (
        ("YES", forecast.probability),
        ("NO", ONE - forecast.probability),
    ):
        price = book.ask(side)
        depth = book.ask_size(side)
        if depth < D(p["min_depth"]):
            continue
        sizing = D(p["quantity"]) * (ONE - forecast.uncertainty)
        quantity = min(sizing, depth).quantize(D("0.01"), rounding="ROUND_DOWN")
        if quantity < ONE:
            continue
        costs = event_fee(price, quantity, book.taker_fee_rate) + quantity * D(
            p["slippage"]
        )
        edge = chance - forecast.uncertainty - price - costs / quantity
        candidates.append((edge, side, price, quantity, costs))
    if not candidates:
        return result("INSUFFICIENT_DEPTH", **forecast_fields)
    edge, side, price, quantity, costs = max(candidates)
    if edge < D(p["min_edge"]):
        return result("EDGE_BELOW_THRESHOLD", edge=edge, **forecast_fields)
    return result(
        "INDEPENDENT_EDGE_AFTER_COSTS",
        side=side,
        orderable=True,
        price=price,
        quantity=quantity,
        exposure=quantity * price,
        payout=quantity,
        edge=edge,
        estimated_costs=costs,
        risks=("FORECAST_ERROR", "PARTIAL_FILL"),
        **forecast_fields,
    )


def market_making(market: Market, p: dict, result: Callable) -> Decision:
    book = market.book
    if book.spread < D(p["min_spread"]):
        return result("SPREAD_TOO_NARROW")
    shallow = min(book.yes_bid_size, book.yes_ask_size)
    deep = max(book.yes_bid_size, book.yes_ask_size)
    if shallow < D(p["min_depth"]):
        return result("INSUFFICIENT_DEPTH")
    if market.adverse or deep / shallow > D(p["max_imbalance"]):
        return result("ADVERSE_SELECTION")
    if not market.inventory.is_finite():
        return result("INVENTORY_UNKNOWN")
    side = "NO" if market.inventory > ZERO else "YES"
    if market.inventory == ZERO and book.bid("YES") > book.bid("NO"):
        side = "NO"
    direction = ONE if side == "YES" else -ONE
    room = D(p["inventory_limit"]) - direction * market.inventory
    quantity = min(D(p["quantity"]), shallow, room).quantize(
        D("0.01"), rounding="ROUND_DOWN"
    )
    if quantity < ONE:
        return result("INVENTORY_LIMIT")
    price = book.bid(side) + book.tick
    if price >= book.ask(side):
        return result("POST_ONLY_NOT_POSSIBLE")
    fee = event_fee(price, quantity, book.maker_fee_rate)
    exit_cost = event_fee(book.ask(side), quantity, book.maker_fee_rate)
    indicative_spread = (
        book.ask(side) - price - (fee + exit_cost) / quantity - D(p["slippage"])
    )
    if indicative_spread < D(p["min_net_spread"]):
        return result("SPREAD_BELOW_COSTS")
    fill_proxy = min(ONE, market.observed_sell_volume / (quantity + shallow))
    return result(
        "POST_ONLY_INVENTORY_CONTROLLED",
        side=side,
        orderable=True,
        price=price,
        quantity=quantity,
        exposure=price * quantity,
        payout=quantity,
        post_only=True,
        estimated_costs=fee,
        risks=("NO_ROUND_TRIP_ASSUMED", "ADVERSE_SELECTION", "PARTIAL_FILL"),
        estimated_fill_probability=fill_proxy,
    )


def logical_scan(market: Market, p: dict, result: Callable) -> Decision:
    book = market.book
    relation = market.relation
    if relation is None:
        return result("NO_VERIFIED_RELATED_MARKET")
    compatible = (
        relation.verified
        and market.rules_id
        and relation.rules_id == market.rules_id
        and relation.other_instrument != market.instrument
        and 0 <= market.timestamp - relation.observed_at <= p["max_leg_age_seconds"]
        and relation.other_book.valid()
        and relation.other_book.fee_schedule_verified
    )
    if not compatible:
        return result("RELATED_MARKET_RULES_OR_FRESHNESS_INVALID")
    # If A implies B, NO(A) + YES(B) pays at least $1. If A and B
    # are exclusive, NO(A) + NO(B) pays at least $1.
    sides = {"PRIMARY_SUBSET": "YES", "MUTUALLY_EXCLUSIVE": "NO"}
    other_side = sides.get(relation.kind)
    if other_side is None:
        return result("RELATION_UNSUPPORTED")
    other = relation.other_book
    depth = min(book.ask_size("NO"), other.ask_size(other_side))
    if depth < D(p["min_depth"]):
        return result("RELATED_MARKET_DEPTH_INSUFFICIENT")
    quantity = min(depth, D(p["quantity"])).quantize(D("0.01"), rounding="ROUND_DOWN")
    price, other_price = book.ask("NO"), other.ask(other_side)
    costs = (
        event_fee(price, quantity, book.taker_fee_rate)
        + event_fee(other_price, quantity, other.taker_fee_rate)
        + 2 * D(p["slippage"]) * quantity
    )
    edge = ONE - price - other_price - costs / quantity
    if edge < D(p["min_edge"]):
        return result("RELATED_MARKET_EDGE_BELOW_THRESHOLD", edge=edge)
    return result(
        "NON_ATOMIC_LOGICAL_OPPORTUNITY",
        side=f"NO+{other_side}",
        price=price + other_price,
        quantity=quantity,
        exposure=quantity * (price + other_price),
        payout=quantity,
        edge=edge,
        estimated_costs=costs,
        other_instrument=relation.other_instrument,
        risks=("NON_ATOMIC_EXECUTION", "UNHEDGED_LEG_RISK"),
    )


KALSHI_RULES = {
    "KALSHI-STRAT-01": probability_edge,
    "KALSHI-STRAT-02": market_making,
    "KALSHI-STRAT-03": logical_scan,
}


def evaluate(
    strategy: str,
    market: Market,
    parameters: Mapping[str, Any] | None = None,
    *,
    estimator: Estimator | None = None,
) -> Decision:
    configuration = validate_parameters(strategy, parameters)

    def result(reason: str, **values: Any) -> Decision:
        return Decision(
            strategy,
            VERSION,
            reason,
            source=market.source,
            timestamp=market.timestamp,
            event=market.event,
            instrument=market.instrument,
            **values,
        )

    if market.exchange != strategy.split("-", 1)[0]:
        return result("EXCHANGE_MISMATCH")
    if market.outcome is not None or (
        market.closes_at is not None and market.timestamp >= market.closes_at
    ):
        return result("MARKET_CLOSED")
    if strategy in DERIV_RULES:
        return deriv_decision(strategy, market, configuration, result)
    if market.book is None or not market.book.valid():
        return result("BOOK_INVALID")
    if not market.book.fee_schedule_verified:
        return result("FEE_SCHEDULE_UNVERIFIED")
    if estimator is not None and strategy == "KALSHI-STRAT-01":
        market = replace(market, forecast=estimator.estimate(market))
    return KALSHI_RULES[strategy](market, configuration, result)
