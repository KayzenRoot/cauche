"""Independent risk decision over account and market observations."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .strategies import Parameter
from .types import CENT, D, FOUR_PLACES, ZERO, Decision, Market, WireRecord, event_fee

LIMITS = {
    "max_order_exposure": Parameter("10", "0.01", "25"),
    "max_strategy_exposure": Parameter("25", "0.01", "75"),
    "max_exchange_exposure": Parameter("35", "0.01", "100"),
    "max_global_exposure": Parameter("50", "0.01", "125"),
    "max_market_exposure": Parameter("20", "0.01", "50"),
    "max_event_exposure": Parameter("25", "0.01", "75"),
    "max_daily_loss": Parameter("20", "0.01", "50"),
    "max_drawdown": Parameter("50", "0.01", "125"),
    "max_open_orders": Parameter(5, 1, 10),
    "max_orders_per_minute": Parameter(3, 1, 10),
    "cooldown_seconds": Parameter(15, 1, 3600),
    "max_spread": Parameter("0.20", "0.01", "0.20"),
    "min_payout_ratio": Parameter("1.40", "1.25", "4.00"),
    "max_age_seconds": Parameter(45, 1, 90),
    "min_depth": Parameter("1", "1", "1000"),
    "max_errors": Parameter(3, 1, 3),
}

DEFAULT_LIMITS = {
    "kill_switch": False,
    **{key: item.default for key, item in LIMITS.items()},
}


def validate_limits(limits: Mapping[str, Any] | None = None) -> dict[str, Any]:
    supplied = {} if limits is None else dict(limits)
    if supplied.keys() - DEFAULT_LIMITS.keys():
        raise ValueError("Unknown risk limit")
    kill_switch = supplied.get("kill_switch", False)
    if not isinstance(kill_switch, bool):
        raise ValueError("kill_switch must be a boolean")
    result = {"kill_switch": kill_switch}
    for key, specification in LIMITS.items():
        try:
            result[key] = specification.validate(
                supplied.get(key, specification.default)
            )
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from error
    return result


@dataclass(frozen=True)
class RiskState:
    global_exposure: Decimal = ZERO
    exchange_exposure: Decimal = ZERO
    strategy_exposure: Decimal = ZERO
    market_exposure: Decimal = ZERO
    event_exposure: Decimal = ZERO
    daily_loss: Decimal = ZERO
    drawdown: Decimal = ZERO
    open_orders: int = 0
    orders_last_minute: int = 0
    last_order_at: int | None = None
    errors: int = 0
    balance_discrepancy: bool = False
    unknown_order: bool = False
    available_balance: Decimal | None = None
    mode: str = "REPLAY"


@dataclass(frozen=True)
class RiskDecision(WireRecord):
    allowed: bool
    code: str
    at: int


def check_risk(
    decision: Decision,
    market: Market,
    limits: Mapping[str, Any] | None = None,
    state: RiskState | None = None,
    *,
    now: int | None = None,
) -> RiskDecision:
    checked_at = market.timestamp if now is None else now

    def blocked(code: str) -> RiskDecision:
        return RiskDecision(False, code, checked_at)

    try:
        rule = validate_limits(limits)
    except (TypeError, ValueError):
        return blocked("RISK_CONFIGURATION_INVALID")
    account = RiskState() if state is None else state
    if account.mode not in {"REPLAY", "PAPER", "BROKER_DEMO"}:
        return blocked("ENVIRONMENT_DENIED")
    if rule["kill_switch"]:
        return blocked("KILL_SWITCH")
    if account.unknown_order:
        return blocked("AMBIGUOUS_ORDER_STATE")
    if account.balance_discrepancy:
        return blocked("BALANCE_DIVERGENCE")
    if account.errors >= rule["max_errors"]:
        return blocked("ERROR_LIMIT")
    if account.mode == "BROKER_DEMO" and market.simulated:
        return blocked("SIMULATED_BROKER_INPUT")
    if not decision.orderable or decision.timestamp != market.timestamp:
        return blocked("INTENT_NOT_ORDERABLE")
    if (decision.instrument, decision.event, decision.source) != (
        market.instrument,
        market.event,
        market.source,
    ):
        return blocked("INTENT_PROVENANCE_MISMATCH")
    if not 0 <= checked_at - market.timestamp <= rule["max_age_seconds"]:
        return blocked("STALE_OR_FUTURE_DATA")
    if market.closes_at is not None and checked_at >= market.closes_at:
        return blocked("MARKET_CLOSED")

    money = (
        account.strategy_exposure,
        account.exchange_exposure,
        account.global_exposure,
        account.market_exposure,
        account.event_exposure,
        account.daily_loss,
        account.drawdown,
        decision.exposure,
        decision.estimated_costs,
        decision.price,
        decision.quantity,
        decision.payout,
    )
    if any(not amount.is_finite() or amount < ZERO for amount in money):
        return blocked("RISK_INPUT_INVALID")
    if (
        min(decision.exposure, decision.price, decision.quantity, decision.payout)
        <= ZERO
    ):
        return blocked("RISK_INPUT_INVALID")
    estimated_costs = decision.estimated_costs
    if market.exchange == "DERIV":
        if decision.side not in {"CALL", "PUT"} or decision.quantity != 1:
            return blocked("PRODUCT_INPUT_INVALID")
        if decision.price != decision.exposure or decision.duration_seconds <= 0:
            return blocked("PRODUCT_INPUT_INVALID")
        if decision.payout / decision.exposure < D(rule["min_payout_ratio"]):
            return blocked("PAYOUT_LIMIT")
        quote = market.deriv_quote(
            decision.side, decision.exposure, decision.duration_seconds
        )
        if quote is None or quote.payout < decision.payout:
            return blocked("QUOTE_UNAVAILABLE")
    elif market.exchange == "KALSHI":
        if decision.side not in {"YES", "NO"} or not ZERO < decision.price < D(1):
            return blocked("PRODUCT_INPUT_INVALID")
        if decision.price * decision.quantity != decision.exposure:
            return blocked("PRODUCT_INPUT_INVALID")
        book = market.book
        if book is None or not book.valid() or not book.fee_schedule_verified:
            return blocked("BOOK_OR_FEE_SCHEDULE_INVALID")
        if book.spread > D(rule["max_spread"]):
            return blocked("SPREAD_LIMIT")
        if book.ask_size(decision.side) < D(rule["min_depth"]):
            return blocked("LIQUIDITY_LIMIT")
        if decision.price.quantize(FOUR_PLACES) != decision.price or (
            decision.quantity.quantize(CENT) != decision.quantity
        ):
            return blocked("FIXED_POINT_PRECISION")
        fee_rate = book.maker_fee_rate if decision.post_only else book.taker_fee_rate
        estimated_costs = max(
            estimated_costs, event_fee(decision.price, decision.quantity, fee_rate)
        )
    else:
        return blocked("UNSUPPORTED_PRODUCT")

    reserved = decision.exposure + estimated_costs
    if account.available_balance is not None:
        if (
            not account.available_balance.is_finite()
            or reserved > account.available_balance
        ):
            return blocked("INSUFFICIENT_BALANCE")
    elif account.mode == "BROKER_DEMO":
        return blocked("BALANCE_UNAVAILABLE")

    exposure_limits = (
        ("ORDER_EXPOSURE_LIMIT", ZERO, "max_order_exposure"),
        ("STRATEGY_EXPOSURE_LIMIT", account.strategy_exposure, "max_strategy_exposure"),
        ("EXCHANGE_EXPOSURE_LIMIT", account.exchange_exposure, "max_exchange_exposure"),
        ("GLOBAL_EXPOSURE_LIMIT", account.global_exposure, "max_global_exposure"),
        ("MARKET_EXPOSURE_LIMIT", account.market_exposure, "max_market_exposure"),
        ("EVENT_EXPOSURE_LIMIT", account.event_exposure, "max_event_exposure"),
    )
    for code, committed, limit in exposure_limits:
        if committed + reserved > D(rule[limit]):
            return blocked(code)
    if account.daily_loss >= D(rule["max_daily_loss"]):
        return blocked("DAILY_LOSS_LIMIT")
    if account.drawdown >= D(rule["max_drawdown"]):
        return blocked("DRAWDOWN_LIMIT")
    if account.open_orders >= rule["max_open_orders"]:
        return blocked("OPEN_ORDERS_LIMIT")
    if account.orders_last_minute >= rule["max_orders_per_minute"]:
        return blocked("ORDER_RATE_LIMIT")
    cooldown = max(rule["cooldown_seconds"], decision.cooldown_seconds)
    if (
        account.last_order_at is not None
        and checked_at - account.last_order_at < cooldown
    ):
        return blocked("COOLDOWN")
    return RiskDecision(True, "ALLOWED", checked_at)
