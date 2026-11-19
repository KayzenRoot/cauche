"""Transactional accounting shared by replay, paper and demo execution."""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import aware, utcnow
from .domain import Decision, Market, RiskState, SimFill, check_risk, settle, simulate_fill
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
        if field.name in {"candles", "confirmation"}:
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
        (row.exchange, row.strategy, row.instrument, row.data.get("event"),
         row.remaining_quantity * row.price)
        for row in open_orders if row.exchange == "KALSHI" and row.remaining_quantity > ZERO
    ]
    reservations += [
        (row.exchange, row.strategy, row.instrument, row.request.get("event"),
         amount((row.request.get("decision") or {}).get("exposure")))
        for row in pending
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


def record_decision(db: Session, bot: Bot, market: Market, decision: Decision, version: int) -> Signal | None:
    slot = market.timestamp
    if not market.simulated:
        slot = market.candles[-1].time if market.exchange == "DERIV" else slot // 15
    identifier = digest(bot.run_id, slot)[:40]
    if db.get(Signal, identifier) is not None:
        return None
    record = Signal(
        **scope(bot, moment(market.timestamp)), id=identifier, version=version,
        direction=decision.side or "NO_SIGNAL", reason=decision.reason, data=decision.as_dict(),
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
         context={"status": decision.status, "side": decision.side, "source": market.source, "time": slot})
    return record


def propose(db: Session, bot: Bot, market: Market, signal: Signal, risk, decision: Decision) -> OrderIntent:
    intent = OrderIntent(
        **scope(bot, moment(market.timestamp)), id=digest(bot.run_id, signal.id),
        signal_id=signal.id, status="PROPOSED", risk=risk.as_dict(),
        request={"decision": decision.as_dict(), "event": market.event, "source": market.source},
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
        currency="USD", data={"event": intent.request.get("event"), **(meta or {})},
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
