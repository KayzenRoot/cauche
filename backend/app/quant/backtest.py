"""Chronological, reproducible backtests against explicitly labeled datasets."""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Mapping

from ..fixtures import dataset
from .indicators import mean
from .risk import RiskState, check_risk, validate_limits
from .simulation import SimFill, settle, simulate_fill
from .strategies import VERSION, evaluate, validate_parameters
from .types import D, ZERO, Decision, Forecast, Market, decimal, wire


def parse_time(value: Any, *, end: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError("Use a UTC epoch timestamp or ISO date/time")
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            start_of_day = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            return int(start_of_day.timestamp()) + (86399 if end else 0)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Datetime must include timezone")
        return int(parsed.timestamp())
    except (OverflowError, ValueError) as error:
        raise ValueError("Invalid UTC date/time filter") from error


@lru_cache(maxsize=128)
def _prepared(
    exchange: str, strategy: str, instrument: str | None, settings: tuple
) -> tuple[tuple[Market, Decision | None], ...]:
    parameters = dict(settings)
    inventory_aware = strategy == "KALSHI-STRAT-02"
    return tuple(
        (market, None if inventory_aware else evaluate(strategy, market, parameters))
        for market in dataset(exchange, instrument)
    )


def _risk_state(
    fills: list[SimFill],
    observation: Market,
    orders: list[int],
    balance: Decimal,
    peak: Decimal,
    daily_change: Decimal,
) -> RiskState:
    def risk(fill: SimFill) -> Decimal:
        return fill.exposure + fill.fee

    total = sum((risk(fill) for fill in fills), ZERO)
    for_market = sum(
        (risk(fill) for fill in fills if fill.instrument == observation.instrument),
        ZERO,
    )
    for_event = sum(
        (risk(fill) for fill in fills if fill.event == observation.event), ZERO
    )
    return RiskState(
        global_exposure=total,
        exchange_exposure=total,
        strategy_exposure=total,
        market_exposure=for_market,
        event_exposure=for_event,
        available_balance=balance - total,
        daily_loss=max(ZERO, -daily_change),
        drawdown=peak - balance,
        open_orders=len(fills),
        orders_last_minute=sum(
            0 <= observation.timestamp - timestamp < 60 for timestamp in orders
        ),
        last_order_at=orders[-1] if orders else None,
    )


def _forecast_history(
    forecasts: dict[str, Forecast], outcomes: dict[str, bool]
) -> list[dict]:
    return [
        {"event": event, **forecast.as_dict(), "observed_outcome": outcomes.get(event)}
        for event, forecast in forecasts.items()
    ]


def _summary(
    trades: list[dict], initial: Decimal, equity: list[dict], forecasts: list[dict]
) -> dict:
    result = [trade["net_pnl"] for trade in trades]
    gain = sum((amount for amount in result if amount > ZERO), ZERO)
    loss = -sum((amount for amount in result if amount < ZERO), ZERO)
    wins = sum(amount > ZERO for amount in result)
    losses_in_a_row = worst_run = 0
    returns = []
    balance = initial
    high_water_mark = initial
    max_drawdown = ZERO
    for amount in result:
        if balance > ZERO:
            returns.append(amount / balance)
        balance += amount
        high_water_mark = max(high_water_mark, balance)
        max_drawdown = max(max_drawdown, high_water_mark - balance)
        losses_in_a_row = losses_in_a_row + 1 if amount < ZERO else 0
        worst_run = max(worst_run, losses_in_a_row)
    average_return = mean(returns) if returns else ZERO
    variance = (
        mean([(value - average_return) ** 2 for value in returns]) if returns else None
    )
    squared_errors = [
        (decimal(item["probability"]) - D(int(item["observed_outcome"]))) ** 2
        for item in forecasts
        if item["observed_outcome"] is not None
    ]
    fee_total = sum((trade["fee"] for trade in trades), ZERO)
    return {
        "net_pnl": gain - loss,
        "gross_pnl": gain - loss + fee_total,
        "initial_balance": initial,
        "closing_balance": balance,
        "roi": (balance - initial) / initial,
        "trade_count": len(result),
        "win_rate": D(wins) / len(result) if result else None,
        "profit_factor": gain / loss if loss else None,
        "expectancy": mean(result) if result else None,
        "max_drawdown": max_drawdown,
        "max_loss_streak": worst_run,
        "return_volatility": variance.sqrt() if variance is not None else None,
        "fees": fee_total,
        "brier_score": mean(squared_errors) if squared_errors else None,
        "scored_forecasts": len(squared_errors),
        "equity": equity,
        "drawdown_basis": "REALIZED_EQUITY",
    }


def _run(
    strategy: str,
    configuration: dict,
    observations: tuple[tuple[Market, Decision | None], ...],
    initial: Decimal,
    costs: dict,
    limits: dict,
) -> dict:
    balance, peak = initial, initial
    day_pnl: defaultdict[int, Decimal] = defaultdict(lambda: ZERO)
    pending: tuple[Decision, str, int] | None = None
    positions: list[SimFill] = []
    trades: list[dict] = []
    orders: list[int] = []
    equity: list[dict] = []
    forecasts: dict[str, Forecast] = {}
    outcomes: dict[str, bool] = {}
    decisions: Counter[str] = Counter()
    risk_blocks: Counter[str] = Counter()
    unfilled_quantity = ZERO
    for observation, prepared in observations:
        timestamp = observation.timestamp
        day = timestamp // 86400
        if observation.outcome is not None:
            outcomes[observation.event] = observation.outcome
        still_open = []
        for fill in positions:
            pnl = settle(fill, observation)
            if pnl is None:
                still_open.append(fill)
                continue
            balance += pnl
            peak = max(peak, balance)
            day_pnl[day] += pnl
            trades.append(
                {
                    **fill.as_dict(),
                    "net_pnl": pnl,
                    "fee": fill.fee,
                    "closed_at": timestamp,
                }
            )
            equity.append({"time": timestamp, "balance": balance})
        positions = still_open
        state = _risk_state(positions, observation, orders, balance, peak, day_pnl[day])
        if pending is not None:
            proposal, event, valid_until = pending
            if event != observation.event or timestamp > valid_until:
                pending = None
            else:
                fill = simulate_fill(proposal, observation, **costs)
                if fill is not None:
                    actual = replace(
                        proposal,
                        timestamp=timestamp,
                        price=fill.price,
                        quantity=fill.quantity,
                        exposure=fill.exposure,
                        payout=fill.payout,
                        estimated_costs=fill.fee,
                    )
                    # The recorded intent itself does not count toward cooldown for its own fill.
                    fill_state = _risk_state(
                        positions,
                        observation,
                        orders[:-1],
                        balance,
                        peak,
                        day_pnl[day],
                    )
                    risk = check_risk(actual, observation, limits, fill_state)
                    if risk.allowed:
                        positions.append(fill)
                        unfilled_quantity += proposal.quantity - fill.quantity
                        state = _risk_state(
                            positions, observation, orders, balance, peak, day_pnl[day]
                        )
                    else:
                        risk_blocks[risk.code] += 1
                    pending = None
                elif not proposal.post_only or observation.outcome is not None:
                    pending = None
        decision = prepared
        if decision is None:
            inventory = sum(
                (
                    fill.quantity if fill.side == "YES" else -fill.quantity
                    for fill in positions
                    if fill.event == observation.event
                ),
                ZERO,
            )
            decision = evaluate(
                strategy, replace(observation, inventory=inventory), configuration
            )
        decisions[decision.status] += 1
        if decision.forecast is not None:
            forecasts.setdefault(observation.event, decision.forecast)
        if pending is not None or not decision.orderable:
            continue
        reserve_costs = {"estimated_costs": decision.estimated_costs}
        if observation.exchange == "DERIV":
            reserve_costs["estimated_costs"] = decision.exposure * (
                costs["fee_rate"] + costs["slippage"]
            )
        risk = check_risk(
            replace(decision, **reserve_costs), observation, limits, state
        )
        if not risk.allowed:
            risk_blocks[risk.code] += 1
            continue
        expiration = timestamp + observation.granularity * (
            2 if decision.post_only else 1
        )
        pending = (decision, observation.event, expiration)
        orders.append(timestamp)

    history = _forecast_history(forecasts, outcomes)
    return {
        "metrics": _summary(trades, initial, equity, history),
        "trades": trades,
        "forecasts": history,
        "closed_orders": len(trades),
        "open_positions": len(positions),
        "unfilled_quantity_after_partial_fills": unfilled_quantity,
        "unfilled_intent_at_end": pending is not None,
        "intent_count": len(orders),
        "decision_counts": dict(decisions),
        "risk_blocks": dict(risk_blocks),
    }


def _split(
    strategy: str,
    configuration: dict,
    timeline: tuple,
    initial: Decimal,
    costs: dict,
    limits: dict,
) -> dict:
    # Keep outcomes of the same Kalshi event inside exactly one partition.
    is_kalshi = strategy.startswith("KALSHI-")
    indices = [0] + [
        index
        for index in range(1, len(timeline))
        if not is_kalshi or timeline[index - 1][0].event != timeline[index][0].event
    ]
    indices.append(len(timeline))
    units = len(indices) - 1
    first, second = indices[(units * 6) // 10], indices[(units * 8) // 10]
    ranges = {
        "train": (0, first),
        "validation": (first, second),
        "test": (second, len(timeline)),
    }
    partitions = {}
    for name, (start, end) in ranges.items():
        segment = timeline[start:end]
        partitions[name] = {
            "start": segment[0][0].timestamp if segment else None,
            "end": segment[-1][0].timestamp if segment else None,
            "observations": len(segment),
            "metrics": _run(strategy, configuration, segment, initial, costs, limits)[
                "metrics"
            ],
        }
    return partitions


def backtest(
    exchange: str,
    strategy: str,
    instrument: str | None = None,
    *,
    initial_balance: Decimal | str = "1000",
    parameters: Mapping[str, Any] | None = None,
    costs: Mapping[str, Any] | None = None,
    risk: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
) -> dict[str, Any]:
    venue = exchange.upper()
    if not strategy.startswith(f"{venue}-"):
        raise ValueError("Strategy belongs to a different exchange")
    config = validate_parameters(strategy, parameters)
    limits = validate_limits(risk)
    balance = decimal(initial_balance)
    if not D(1) <= balance <= D("10000000") or balance != balance.quantize(D("0.01")):
        raise ValueError("Initial balance must use cents and be between 1 and 10000000")
    simulation_costs = {"slippage": ZERO, "fee_rate": ZERO}
    if costs is not None:
        if costs.keys() - simulation_costs.keys():
            raise ValueError("Unknown simulation cost parameter")
        simulation_costs.update({key: decimal(value) for key, value in costs.items()})
    if any(not ZERO <= value <= D("0.5") for value in simulation_costs.values()):
        raise ValueError("Simulation costs outside supported range")
    first, last = parse_time(start), parse_time(end, end=True)
    if first is not None and last is not None and first > last:
        raise ValueError("Start date must be before or equal to end date")
    prepared = _prepared(venue, strategy, instrument, tuple(config.items()))
    timeline = tuple(
        (market, decision)
        for market, decision in prepared
        if (first is None or market.timestamp >= first)
        and (last is None or market.timestamp <= last)
    )
    if not timeline:
        raise ValueError("No fixture data in selected period")
    full = _run(strategy, config, timeline, balance, simulation_costs, limits)
    stress_costs = {
        **simulation_costs,
        "slippage": min(D("0.5"), simulation_costs["slippage"] + D("0.01")),
    }
    sensitivity = _run(strategy, config, timeline, balance, stress_costs, limits)[
        "metrics"
    ]
    partitions = _split(strategy, config, timeline, balance, simulation_costs, limits)
    return wire(
        {
            "exchange": venue,
            "strategy": strategy,
            "strategy_version": VERSION,
            "instrument": timeline[0][0].instrument,
            "mode": "REPLAY",
            "source": "SIMULATED_FIXTURE",
            "dataset_version": "fixture-1",
            "start": timeline[0][0].timestamp,
            "end": timeline[-1][0].timestamp,
            "observations": len(timeline),
            "parameters": config,
            "costs": simulation_costs,
            "risk": limits,
            "temporal_partitions": partitions,
            "out_of_sample": partitions["test"],
            "slippage_sensitivity": {"costs": stress_costs, "metrics": sensitivity},
            "fit_method": "FIXED_PARAMETERS_NO_TRAINING",
            **full,
        }
    )
