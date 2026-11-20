"""Transactional accounting shared by replay, paper and demo execution."""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import aware, utcnow
from .domain import Decision, Market, RiskState, SimFill, check_risk, settle, simulate_fill, validate_limits
from .quant.indicators import adx, atr, closed_bars
from .serialization import jsonable
from .events import event
from .models import (
    BalanceSnapshot,
    Bot,
    Fee,
    Fill,
    MarketCandle,
    MarketSnapshot,
    Order,
    OrderIntent,
    Position,
    ProbabilityEstimate,
    RiskViolation,
    Settlement,
    Signal,
    SystemEvent,
)

ZERO = Decimal("0")
INITIAL_BALANCE = Decimal("1000")
PENDING = ("PROPOSED", "SUBMITTING", "UNKNOWN")
OPEN_ORDERS = ("OPEN", "ACCEPTED", "PARTIAL", "CANCEL_PENDING")


def moment(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def amount(value, default="0") -> Decimal:
    return Decimal(str(default if value is None else value))


def scope(record, timestamp: datetime) -> dict:
    return {
        key: getattr(record, key) for key in ("mode", "exchange", "strategy", "instrument", "run_id")
    } | {"timestamp": timestamp}


def emit(db: Session, record, category: str, code: str, message: str, **fields) -> None:
    event(
        db, category, code, message,
        mode=record.mode, exchange=record.exchange, strategy=record.strategy,
        instrument=record.instrument, correlation_id=record.run_id, **fields,
    )


def digest(*parts) -> str:
    return sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def save_market(db: Session, bot: Bot, market: Market, *, tick: dict | None = None) -> None:
    recorded = scope(bot, moment(market.timestamp))
    values = {}
    for field in fields(market):
        if field.name in {"candles", "confirmation", "book_levels"}:
            continue
        value = getattr(market, field.name)
        if isinstance(value, tuple):
            value = [item.as_dict() for item in value]
        elif hasattr(value, "as_dict"):
            value = value.as_dict()
        elif isinstance(value, Decimal):
            value = str(value)
        values[field.name] = value
    if tick:
        values.update(mark=tick["price"], stream_timestamp=tick["observed_at"], stream_source=tick["source"])
    db.add(MarketSnapshot(**recorded, source=market.source, status="CURRENT", data=values))
    existing = set(db.scalars(select(MarketCandle.time).where(
        MarketCandle.mode == bot.mode, MarketCandle.exchange == bot.exchange,
        MarketCandle.instrument == bot.instrument, MarketCandle.source == market.source,
        MarketCandle.timeframe == market.granularity,
        MarketCandle.time.in_([bar.time for bar in market.candles]),
    )))
    for bar in market.candles:
        if bar.time not in existing:
            db.add(MarketCandle(
                **recorded, source=market.source, time=bar.time, timeframe=market.granularity,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume,
            ))


def check_context(
    db: Session, bot: Bot, market: Market, available: Decimal | None, *, exclude_intent: str = "",
) -> RiskState:
    positions = db.execute(select(Position, Order).join(Order, Order.id == Position.order_id).where(
        Position.mode == bot.mode, Position.status == "OPEN", Position.currency == "USD",
    )).all()
    open_orders = db.scalars(select(Order).where(Order.mode == bot.mode, Order.status.in_(OPEN_ORDERS))).all()
    pending = db.scalars(select(OrderIntent).where(OrderIntent.mode == bot.mode, OrderIntent.status.in_(PENDING), OrderIntent.id != exclude_intent)).all()
    reservations = [
        (row.exchange, row.strategy, row.instrument, order.data.get("event"), row.exposure)
        for row, order in positions
    ]
    reservations += [
        (
            row.exchange, row.strategy, row.instrument, row.data.get("event"),
            row.remaining_quantity * row.price + (
                amount(row.data.get("estimated_costs")) * row.remaining_quantity / row.quantity
                if row.quantity > ZERO else ZERO
            ),
        )
        for row in open_orders if row.exchange == "KALSHI" and row.remaining_quantity > ZERO
    ]
    reservations += [
        (row.exchange, row.strategy, row.instrument, row.request.get("event"),
         amount(row.request["decision"].get("exposure")) + amount(row.request["decision"].get("estimated_costs")))
        for row in pending if "decision" in row.request
    ]
    observed = moment(market.timestamp)
    day_start = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_loss = -sum(db.scalars(select(Settlement.net_pnl).where(
        Settlement.mode == bot.mode,
        Settlement.timestamp >= day_start, Settlement.timestamp <= observed,
        Settlement.currency == "USD",
        *([] if bot.mode == "BROKER_DEMO" else [Settlement.run_id == bot.run_id]),
    )), ZERO)
    recent = list(db.scalars(select(OrderIntent.timestamp).where(
        OrderIntent.mode == bot.mode, OrderIntent.id != exclude_intent,
        *([] if bot.mode == "BROKER_DEMO" else [OrderIntent.run_id == bot.run_id]),
        OrderIntent.timestamp >= observed - timedelta(days=1), OrderIntent.timestamp <= observed,
    )))
    snapshot = last_balance(db, bot)
    prior_failures = db.scalar(select(func.count()).select_from(SystemEvent).where(
        SystemEvent.correlation_id == bot.run_id,
        SystemEvent.type == "ERROR",
        SystemEvent.timestamp > utcnow() - timedelta(minutes=5),
    )) or 0
    return RiskState(
        global_exposure=sum((row[4] for row in reservations), ZERO),
        exchange_exposure=sum((row[4] for row in reservations if row[0] == bot.exchange), ZERO),
        strategy_exposure=sum((row[4] for row in reservations if row[1] == bot.strategy), ZERO),
        market_exposure=sum((row[4] for row in reservations if row[0] == bot.exchange and row[2] == bot.instrument), ZERO),
        event_exposure=sum((row[4] for row in reservations if row[0] == bot.exchange and row[3] == market.event), ZERO),
        daily_loss=max(ZERO, daily_loss), drawdown=snapshot.drawdown if snapshot else ZERO,
        open_orders=len(open_orders) + len(pending),
        orders_last_minute=sum(aware(ts).timestamp() >= market.timestamp - 60 for ts in recent),
        last_order_at=int(aware(max(recent)).timestamp()) if recent else None,
        errors=prior_failures,
        unknown_order=any(row.status in {"UNKNOWN", "SUBMITTING"} for row in pending),
        available_balance=available, mode=bot.mode,
    )


def last_balance(db: Session, bot: Bot) -> BalanceSnapshot | None:
    return db.scalar(select(BalanceSnapshot).where(
        BalanceSnapshot.run_id == bot.run_id,
    ).order_by(BalanceSnapshot.id.desc()).limit(1))


def save_balance(
    db: Session, bot: Bot, market: Market, *, broker_balance: Decimal | None = None,
) -> Decimal | None:
    settlements = list(db.scalars(select(Settlement).where(Settlement.run_id == bot.run_id)))
    positions = list(db.scalars(select(Position).where(
        Position.run_id == bot.run_id, Position.status == "OPEN",
    )))
    fees = sum(db.scalars(select(Fee.amount).where(Fee.run_id == bot.run_id)), ZERO)
    realized = sum((row.net_pnl for row in settlements), ZERO)
    initial = INITIAL_BALANCE
    previous = last_balance(db, bot)
    if bot.mode == "BROKER_DEMO":
        if broker_balance is None:
            return None
        initial = previous.initial_balance if previous else broker_balance
    open_cost = sum((row.exposure for row in positions), ZERO)
    fees_open = fees - sum((row.fees for row in settlements), ZERO)
    balance = initial + realized - open_cost - fees_open if broker_balance is None else broker_balance
    marks = []
    for position in positions:
        order = db.get(Order, position.order_id)
        if market.book and position.exchange == "KALSHI" and order.data.get("event") == market.event:
            mark = market.book.bid(position.side) * position.quantity - position.exposure
            position.unrealized_pnl = mark
            marks.append(mark)
        else:
            position.unrealized_pnl = None
    unrealized = sum(marks, ZERO) if len(marks) == len(positions) else None
    equity = balance + open_cost + (unrealized or ZERO)
    peak = db.scalar(select(func.max(BalanceSnapshot.equity)).where(
        BalanceSnapshot.run_id == bot.run_id,
    )) or initial
    resting = db.scalar(select(Order.id).where(
        Order.run_id == bot.run_id, Order.remaining_quantity > ZERO,
    ).limit(1))
    if bot.mode == "BROKER_DEMO" and previous and not resting:
        # Reconcile against the broker's own cash. Unexpected changes fail closed;
        # only one demo run can own exposure and off-platform activity is unsupported.
        expected = initial + realized - open_cost - fees_open
        if abs(balance - expected) > Decimal("0.01") or db.scalar(
            select(RiskViolation.id).where(
                RiskViolation.run_id == bot.run_id, RiskViolation.code == "BALANCE_DISCREPANCY",
            ).limit(1)
        ) is not None:
            if bot.state != "RISK_BLOCKED":
                risk_violation(db, bot, "BALANCE_DISCREPANCY", None, {"source": market.source})
            bot.state = "RISK_BLOCKED"
            bot.desired_state = "PAUSED"
            return None
    db.add(BalanceSnapshot(
        **scope(bot, moment(market.timestamp)), source="OFFICIAL_DEMO" if broker_balance is not None else market.source,
        currency="USD", initial_balance=initial, balance=balance, equity=equity,
        realized_pnl=realized, unrealized_pnl=unrealized, fees=fees,
        drawdown=max(peak, initial, equity) - equity,
    ))
    return balance


def risk_violation(db: Session, bot: Bot, code: str, signal_id: str | None, details: dict) -> None:
    db.add(RiskViolation(
        **scope(bot, utcnow()), code=code, message=code, signal_id=signal_id, data=details,
    ))
    emit(db, bot, "RISK", code, code, severity="WARNING", context=details)


def entry_gate(
    decision: Decision, market: Market, *, deriv_calibration: dict | None = None,
) -> tuple[Decision, dict]:
    """Preserve synthetic scenarios and fail closed on unproven live expectancy."""
    probability, uncertainty = None, None
    forecast = decision.forecast
    if forecast and forecast.is_usable(market) and decision.side in {"YES", "NO"}:
        probability = forecast.probability if decision.side == "YES" else 1 - forecast.probability
        uncertainty = forecast.uncertainty
    if (
        market.exchange == "DERIV" and decision.side in {"CALL", "PUT"}
        and deriv_calibration and deriv_calibration["status"] == "VALIDATED"
    ):
        quote = market.deriv_quote(decision.side, decision.price, decision.duration_seconds)
        if (
            quote and quote.observed_at is not None and 0 <= market.timestamp - quote.observed_at <= 10
            and quote.as_dict() == deriv_calibration["quote"]
            and deriv_calibration["holdout_until"] < moment(market.timestamp)
        ):
            probability = deriv_calibration["probability_of_win"]
            uncertainty = deriv_calibration["uncertainty"]
    expected = None
    if probability is not None and uncertainty is not None:
        expected = max(ZERO, probability - uncertainty) * decision.payout - decision.exposure - decision.estimated_costs

    stats = jsonable({
        "candidate_side": decision.side,
        "candidate_reason": decision.reason,
        "probability_of_win": probability,
        "uncertainty": uncertainty,
        "conservative_net_ev": expected,
        "unit": "USD_PER_ORDER",
        "calibration": deriv_calibration,
    })
    if market.simulated or not decision.orderable:
        return decision, stats
    if expected is None:
        return replace(decision, side=None, orderable=False, reason="CALIBRATED_EV_UNAVAILABLE"), stats
    if expected <= ZERO:
        return replace(decision, side=None, orderable=False, reason="NET_EV_NOT_POSITIVE"), stats
    return decision, stats


def portfolio_pause(state: RiskState, limits: dict) -> str | None:
    rule = validate_limits(limits)
    if state.daily_loss >= amount(rule["max_daily_loss"]):
        return "DAILY_LOSS_LIMIT"
    if state.drawdown >= amount(rule["max_drawdown"]):
        return "DRAWDOWN_LIMIT"
    return None


def deriv_regime(market: Market) -> dict | None:
    if market.exchange != "DERIV":
        return None
    bars = closed_bars(market.candles, market.timestamp, market.granularity)
    if not bars:
        return {"name": "UNAVAILABLE"}
    strength = adx(bars)
    volatility = atr(bars)
    regime = "UNAVAILABLE"
    if strength is not None:
        if strength >= 25:
            regime = "TREND"
        elif strength <= 20:
            regime = "RANGE"
        else:
            regime = "TRANSITION"
    return jsonable({
        "name": regime,
        "adx_14": strength,
        "relative_atr_14": volatility / bars[-1].close if volatility is not None else None,
        "closed_bar_at": bars[-1].time,
        "utc_hour": moment(market.timestamp).hour,
    })



def _wilson_lower(wins: int, count: int) -> Decimal:
    ratio = Decimal(wins) / count
    z = Decimal("1.96")
    correction = z * z / count
    radius = z * ((ratio * (1 - ratio) + correction / 4) / count).sqrt()
    return max(ZERO, (ratio + correction / 2 - radius) / (1 + correction))


def fit_deriv_calibration(samples: list[tuple[datetime, datetime, Decimal]]) -> dict:
    """Use disjoint observations and retain the final chronological third as a holdout."""
    independent = []
    for opened, resolved, net in sorted(samples):
        if resolved > opened and (not independent or opened > independent[-1][1]):
            independent.append((opened, resolved, net))
    boundary = 2 * len(independent) // 3
    train, holdout = independent[:boundary], independent[boundary:]
    counts = {"train_count": len(train), "holdout_count": len(holdout)}
    if len(train) < 40 or len(holdout) < 20 or min(
        len({time.date() for time, _, _ in fold}) for fold in (train, holdout)
    ) < 3:
        return {"status": "INSUFFICIENT_OFFICIAL_DEMO_DATA", **counts}
    train_wins, holdout_wins = (sum(row[2] > ZERO for row in fold) for fold in (train, holdout))
    train_pnl, holdout_pnl = (sum((row[2] for row in fold), ZERO) for fold in (train, holdout))
    probability = Decimal(train_wins) / len(train)
    lower = min(_wilson_lower(train_wins, len(train)), _wilson_lower(holdout_wins, len(holdout)))
    brier = sum((probability - Decimal(int(row[2] > ZERO))) ** 2 for row in holdout) / len(holdout)
    # A model that loses to the coin-flip reference or loses money across the
    # independently measured holdout cannot enable entries.
    holdout_evidence = {"holdout_brier": brier, "holdout_net_pnl": holdout_pnl, **counts}
    if brier >= Decimal("0.25") or holdout_wins * 2 <= len(holdout) or min(train_pnl, holdout_pnl) <= ZERO:
        return {"status": "HOLDOUT_CALIBRATION_FAILED", **holdout_evidence}
    return {
        "status": "VALIDATED", "source": "PRIOR_OFFICIAL_DEMO_SETTLEMENTS",
        "model": "DERIV_EMPIRICAL_WILSON_V1", "probability_of_win": probability,
        "uncertainty": probability - lower,
        "trained_until": train[-1][1], "holdout_until": holdout[-1][1], **holdout_evidence,
    }


def deriv_settlement_calibration(
    db: Session, bot: Bot, market: Market, decision: Decision, version: int, parameters: dict,
) -> dict | None:
    if market.simulated or market.exchange != "DERIV" or not decision.orderable:
        return None
    quote = market.deriv_quote(decision.side, decision.price, decision.duration_seconds)
    regime = deriv_regime(market)
    if (
        quote is None or quote.observed_at is None or not 0 <= market.timestamp - quote.observed_at <= 10
        or not regime or regime["name"] == "UNAVAILABLE"
    ):
        return {"status": "UNALIGNED_CONTRACT_OR_REGIME"}
    rows = db.execute(
        select(Signal, OrderIntent, Order, Settlement)
        .join(OrderIntent, OrderIntent.signal_id == Signal.id)
        .join(Order, Order.intent_id == OrderIntent.id)
        .join(Settlement, Settlement.order_id == Order.id)
        .where(
            Signal.mode == "BROKER_DEMO", Signal.exchange == "DERIV", Signal.instrument == bot.instrument,
            Signal.strategy == bot.strategy, Signal.version == version,
            Order.mode == "BROKER_DEMO", Order.source == "OFFICIAL_DEMO",
            Order.side == decision.side, Order.price == decision.price, Order.quantity == 1,
            Settlement.mode == "BROKER_DEMO", Settlement.source == "OFFICIAL_DEMO",
            Settlement.currency == "USD", Settlement.timestamp < moment(market.timestamp),
        )
        .order_by(Signal.timestamp.desc(), Signal.id.desc()).limit(500)
    )
    samples = []
    hour_bucket = regime["utc_hour"] // 4
    terms = jsonable(parameters)
    for signal, intent, order, settlement in rows:
        evidence = signal.data.get("evidence") or {}
        recorded_regime = evidence.get("market_regime") or {}
        hour = recorded_regime.get("utc_hour")
        original = intent.request.get("decision") or {}
        offered = order.data.get("broker_quoted_payout") or original.get("payout")
        if (
            evidence.get("parameters") != terms or recorded_regime.get("name") != regime["name"]
            or type(hour) is not int or hour // 4 != hour_bucket
            or original.get("duration_seconds") != decision.duration_seconds or not offered
            or not quote.payout * Decimal("0.9") <= amount(offered) <= quote.payout * Decimal("1.1")
        ):
            continue
        won = settlement.payout == amount(offered) and settlement.net_pnl > ZERO
        if won or (settlement.payout == ZERO and settlement.net_pnl < ZERO):
            samples.append((aware(signal.timestamp), aware(settlement.timestamp), settlement.net_pnl))
    return {**fit_deriv_calibration(samples), "quote": quote.as_dict(), "regime": regime["name"]}


def market_resolution(market: Market) -> dict | None:
    if market.exchange == "KALSHI" and market.outcome is not None:
        return {
            "outcome": "YES" if market.outcome else "NO",
            "observed_at": market.timestamp,
            "source": market.source,
        }
    return None


def resolve_market_signals(db: Session, bot: Bot, market: Market) -> None:
    resolution = market_resolution(market)
    if resolution is None:
        return
    signals = db.scalars(select(Signal).where(
        Signal.run_id == bot.run_id, Signal.instrument == market.instrument,
        Signal.timestamp <= moment(market.timestamp),
        Signal.data["evidence"]["event"].as_string() == market.event,
    ))
    for signal in signals:
        if "resolution" not in signal.data:
            signal.data = {**signal.data, "resolution": resolution}


def record_decision(
    db: Session, bot: Bot, market: Market, decision: Decision, version: int, *,
    parameters: dict, expectancy: dict, shadows: list[dict] | None = None,
    observation_received_at: datetime | None = None,
) -> Signal | None:
    slot = market.timestamp
    if not market.simulated:
        slot = market.candles[-1].time if market.exchange == "DERIV" and market.candles else slot // 15
    identifier = digest(bot.run_id, slot)[:40]
    if db.get(Signal, identifier) is not None:
        return None
    # For Deriv the exact closed candles are actual strategy inputs. Kalshi uses
    # executable top-of-book depth, market rules, related book and its forecast.
    evidence = {
        "observed_at": market.timestamp,
        "received_at": jsonable(observation_received_at),
        "recorded_at": jsonable(utcnow()),
        "source": market.source,
        "event": market.event,
        "slot": slot,
        "rules_id": market.rules_id,
        "closes_at": market.closes_at,
        "book": market.book.as_dict() if market.book else None,
        "book_levels": [level.as_dict() for level in market.book_levels],
        "contracts": [quote.as_dict() for quote in market.contracts],
        "model": decision.forecast.as_dict() if decision.forecast else None,
        "related_market": market.relation.as_dict() if market.relation else None,
        "market_regime": deriv_regime(market),
        "max_loss": jsonable(decision.exposure + decision.estimated_costs),
        "parameters": jsonable(parameters),
        "inventory": jsonable(market.inventory),
        "observed_sell_volume": jsonable(market.observed_sell_volume),
        "adverse": market.adverse,
    }
    if market.exchange == "DERIV":
        evidence["candles"] = [
            candle.as_dict() for candle in market.candles if candle.closed and candle.time <= market.timestamp
        ]
        evidence["confirmation"] = [
            candle.as_dict() for candle in market.confirmation if candle.closed and candle.time <= market.timestamp
        ]
    resolution = market_resolution(market)
    record = Signal(
        **scope(bot, moment(market.timestamp)), id=identifier, version=version,
        direction=decision.side or "NO_SIGNAL", reason=decision.reason,
        data={
            **decision.as_dict(), "evidence": evidence,
            "expectancy": expectancy, "shadow": shadows or [],
            **({"resolution": resolution} if resolution else {}),
            "market_time": market.candles[-1].time if market.exchange == "DERIV" and market.candles else market.timestamp,
        },
    )
    db.add(record)
    db.flush()
    if decision.forecast:
        forecast = decision.forecast
        db.add(ProbabilityEstimate(
            **scope(bot, moment(market.timestamp)), model=forecast.source, version=forecast.version,
            source=market.source, probability=forecast.probability, uncertainty=forecast.uncertainty,
            data={**forecast.as_dict(), "event": market.event},
        ))
    emit(db, bot, "SIGNAL" if decision.side else "STRATEGY", decision.reason, decision.reason,
         context={"signal_id": record.id, "status": decision.status, "side": decision.side,
                  "source": market.source, "time": market.timestamp})
    return record


def propose(db: Session, bot: Bot, market: Market, signal: Signal, risk, decision: Decision) -> OrderIntent:
    intent = OrderIntent(
        **scope(bot, moment(market.timestamp)), id=digest(bot.run_id, signal.id),
        signal_id=signal.id, status="PROPOSED", risk=risk.as_dict(),
        request={
            "decision": decision.as_dict(), "event": market.event, "source": market.source,
            "decision_recorded_at": signal.data["evidence"]["recorded_at"],
            "expectancy": signal.data["expectancy"],
        },
    )
    db.add(intent)
    db.flush()
    emit(db, bot, "ORDER", "ORDER_PROPOSED", "Order intent persisted", order_id=intent.id)
    return intent


def new_order(
    db: Session, intent: OrderIntent, decision: Decision, *, status: str,
    source: str, broker_id: str | None = None, price: Decimal | None = None,
    remaining: Decimal = ZERO, meta: dict | None = None,
) -> Order:
    order = Order(
        **scope(intent, intent.timestamp), id=intent.id, intent_id=intent.id, broker_id=broker_id,
        status=status, side=decision.side, quantity=decision.quantity, remaining_quantity=remaining,
        price=decision.price if price is None else price, exposure=decision.exposure, source=source,
        currency="USD", data={
            "event": intent.request.get("event"),
            "estimated_costs": str(decision.estimated_costs),
            **(meta or {}),
        },
    )
    db.add(order)
    db.flush()
    return order


def record_fill(
    db: Session, order: Order, *, key: str, quantity: Decimal, price: Decimal, fee: Decimal,
    timestamp: datetime, broker_id: str | None = None,
) -> None:
    identifier = digest(order.id, key)
    if db.get(Fill, identifier) is not None:
        return
    row = scope(order, timestamp)
    db.add(Fill(
        **row, id=identifier, order_id=order.id, broker_id=broker_id, quantity=quantity, price=price,
        fee=fee, source=order.source, currency="USD",
    ))
    if fee > ZERO:
        db.add(Fee(
            **row, id=identifier, order_id=order.id, amount=fee, source=order.source, currency="USD",
        ))
    position = db.get(Position, order.id)
    if position is None:
        db.add(Position(
            **row, id=order.id, order_id=order.id, side=order.side, status="OPEN",
            quantity=quantity, exposure=quantity * price, source=order.source, currency="USD",
        ))
    else:
        position.quantity += quantity
        position.exposure += quantity * price
    emit(db, order, "FILL", "FILL_CONFIRMED", "Fill recorded", order_id=order.id,
         context={"source": order.source, "quantity": str(quantity), "price": str(price)})


def record_settlement(
    db: Session, order: Order, *, gross: Decimal, payout: Decimal, timestamp: datetime,
) -> None:
    if db.get(Settlement, order.id) is not None:
        return
    position = db.get(Position, order.id)
    if position is None:
        return
    fees = sum(db.scalars(select(Fee.amount).where(Fee.order_id == order.id)), ZERO)
    db.add(Settlement(
        **scope(order, timestamp), id=order.id, order_id=order.id, source=order.source,
        gross_pnl=gross, fees=fees, net_pnl=gross - fees, payout=payout, currency="USD",
    ))
    position.status = "SETTLED"
    position.unrealized_pnl = None
    order.status = "SETTLED"
    order.remaining_quantity = ZERO
    emit(db, order, "SETTLEMENT", "SETTLED", "Settlement recorded", order_id=order.id,
         context={"source": order.source, "net_pnl": str(gross - fees), "payout": str(payout)})


def settle_simulation(db: Session, bot: Bot, market: Market) -> None:
    active = db.scalars(select(Order).where(Order.run_id == bot.run_id, Order.status == "FILLED")).all()
    for order in active:
        filled = SimFill.from_dict(order.data["sim_fill"])
        net = settle(filled, market)
        if net is None:
            continue
        gross = net + filled.fee
        record_settlement(
            db, order, gross=gross, payout=filled.exposure + gross, timestamp=moment(market.timestamp),
        )
    if market.outcome is not None:
        estimates = db.scalars(select(ProbabilityEstimate).where(
            ProbabilityEstimate.run_id == bot.run_id, ProbabilityEstimate.outcome.is_(None),
        ))
        for estimate in estimates:
            if estimate.data.get("event") != market.event:
                continue
            estimate.outcome = market.outcome
            estimate.brier_score = (estimate.probability - Decimal(int(market.outcome))) ** 2


def execute_simulation(
    db: Session, bot: Bot, market: Market, limits: dict, *, can_open: bool,
) -> None:
    pending = db.scalars(select(OrderIntent).where(
        OrderIntent.run_id == bot.run_id, OrderIntent.status == "PROPOSED",
    )).all()
    for intent in pending:
        if market.timestamp <= intent.timestamp.replace(tzinfo=UTC).timestamp():
            continue
        decision = Decision.from_dict(intent.request["decision"])
        age = market.timestamp - int(intent.timestamp.replace(tzinfo=UTC).timestamp())
        if not can_open or intent.request.get("event") != market.event:
            intent.status = "CANCELLED"
            emit(db, intent, "ORDER", "ORDER_CANCELLED", "Opening exposure is paused", order_id=intent.id)
            continue
        fill = simulate_fill(decision, market)
        if fill is None:
            if age >= market.granularity:
                intent.status = "EXPIRED"
                emit(db, intent, "ORDER", "ORDER_EXPIRED", "No executable simulated fill", order_id=intent.id)
            continue
        previous = last_balance(db, bot)
        available = previous.balance if previous else INITIAL_BALANCE
        state = check_context(db, bot, market, available, exclude_intent=intent.id)
        assessment = check_risk(replace(decision, timestamp=market.timestamp), market, limits, state)
        if not assessment.allowed:
            intent.status = "CANCELLED"
            risk_violation(db, bot, assessment.code, intent.signal_id, assessment.as_dict())
            continue
        # The committed risk budget remains an upper bound at the next observation.
        if fill.exposure + fill.fee > decision.exposure + decision.estimated_costs:
            intent.status = "CANCELLED"
            emit(db, intent, "RISK", "SIMULATION_BUDGET_CHANGED", "Execution costs exceeded reserved budget",
                 order_id=intent.id, severity="WARNING")
            continue
        filled_order = new_order(
            db, intent, decision, status="FILLED", source=f"{bot.mode}_{market.source}"[:40],
            price=fill.price, meta={"sim_fill": fill.as_dict(), "unfilled_quantity": str(decision.quantity - fill.quantity)},
        )
        filled_order.exposure = fill.exposure
        intent.status = "CONFIRMED"
        record_fill(
            db, filled_order, key="simulation", quantity=fill.quantity, price=fill.price,
            fee=fill.fee, timestamp=moment(market.timestamp),
        )


def execution_inventory(db: Session, bot: Bot, market: Market) -> tuple[Decimal, bool]:
    rows = db.execute(select(Position, Order).join(Order, Order.id == Position.order_id).where(
        Position.run_id == bot.run_id, Position.status == "OPEN",
    ))
    inventory = sum((
        position.quantity if position.side == "YES" else -position.quantity
        for position, order in rows if order.data.get("event") == market.event
    ), ZERO)
    resting = db.scalar(select(Order.id).where(
        Order.run_id == bot.run_id, Order.remaining_quantity > ZERO,
    ).limit(1)) is not None
    return inventory, resting
