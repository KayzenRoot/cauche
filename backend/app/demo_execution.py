"""Demo-only broker dispatch and reconciliation against official outcomes."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import aware, utcnow
from .domain import Decision
from .execution import amount, digest, emit, moment, new_order, record_fill, record_settlement
from .integrations import Deriv, Kalshi, VenueError
from .market_data import deriv_symbol
from .models import Bot, ExchangeConnection, Fee, Fill, Order, OrderIntent, Position, RiskConfig, StrategyConfig
from .quant.types import decimal
from .security import decrypt_credentials

ZERO = Decimal("0")
BROKER_SOURCE = "OFFICIAL_DEMO"


def client(connection: ExchangeConnection):
    if not connection.enabled or not connection.ciphertext:
        raise ValueError("DEMO_NOT_CONFIGURED")
    credentials = decrypt_credentials(connection.ciphertext)
    if connection.exchange == "DERIV":
        return Deriv(credentials["app_id"], credentials["token"], credentials.get("account_id"))
    if connection.exchange == "KALSHI":
        return Kalshi(credentials["key_id"], credentials["private_key"])
    raise ValueError("UNSUPPORTED_EXCHANGE")


async def has_unmanaged_exposure(venue) -> bool:
    if isinstance(venue, Deriv):
        return bool(await venue.portfolio())
    positions = await venue.positions()
    return any(position["quantity"] != ZERO or position["exposure"] != ZERO for position in positions) or bool(
        await venue.open_orders()
    )


def confirmation_amount(value, *, stored: bool = True, signed: bool = False) -> Decimal:
    """Validate normalized exact amounts before any accounting mutation."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    try:
        number = decimal(value)
    except ValueError:
        raise ValueError("INVALID_BROKER_CONFIRMATION") from None
    # MONEY columns use Numeric(24, 8). Reject rather than silently round.
    if (not signed and number < ZERO) or (stored and (
        number.copy_abs() >= Decimal("1e16") or number != number.quantize(Decimal("0.00000001"))
    )):
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    return number


def acknowledge(db: Session, intent: OrderIntent, result: dict) -> Order:
    if not isinstance(result, dict):
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    broker_id = result.get("broker_id")
    broker_status = result.get("status")
    if (
        not isinstance(broker_id, str) or not broker_id.strip()
        or broker_id != broker_id.strip() or len(broker_id) > 100
        or not isinstance(broker_status, str)
    ):
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    decision = Decision.from_dict(intent.request["decision"])
    state = broker_status.upper().replace("CANCELED", "CANCELLED")
    if state not in {"ACCEPTED", "OPEN", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    # Deriv's normalized buy acknowledgement is OPEN, never a rejection or cancellation.
    # Do not create an unconditional fill for any other state.
    if intent.exchange == "DERIV" and state != "OPEN":
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    if (
        intent.exchange not in {"DERIV", "KALSHI"}
        or result.get("source") != f"{intent.exchange}_DEMO"
        or result.get("client_order_id") != intent.id
    ):
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    price = confirmation_amount(result.get("price"))
    quantity = confirmation_amount(result.get("quantity"))
    if not ZERO < price <= decision.price or quantity != decision.quantity or quantity <= ZERO:
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    if intent.exchange == "DERIV":
        if result.get("currency") != "USD":
            raise ValueError("UNSUPPORTED_CURRENCY")
        remaining = confirmation_amount(result.get("remaining_quantity", ZERO))
        filled = confirmation_amount(result.get("filled_quantity", Decimal("1")))
        fee = confirmation_amount(result.get("fee", ZERO))
        if quantity != 1 or filled != 1 or remaining != ZERO:
            raise ValueError("INVALID_BROKER_CONFIRMATION")
    else:
        if (
            result.get("instrument") != intent.instrument or result.get("side") != decision.side
            or price >= 1
        ):
            raise ValueError("INVALID_BROKER_CONFIRMATION")
        remaining = confirmation_amount(result.get("remaining_quantity"))
        filled = confirmation_amount(result.get("filled_quantity"))
        fee = confirmation_amount(result.get("fee"), stored=False)
        if filled + remaining > quantity:
            raise ValueError("INVALID_BROKER_CONFIRMATION")
        if state in {"OPEN", "ACCEPTED"}:
            consistent = filled == ZERO and remaining > ZERO
        elif state == "PARTIAL":
            consistent = ZERO < filled < quantity
        elif state == "FILLED":
            consistent = filled == quantity and remaining == ZERO
        else:
            consistent = filled == ZERO and remaining == ZERO
        if not consistent:
            raise ValueError("INVALID_BROKER_CONFIRMATION")
    order = new_order(
        db, intent, decision, status=state, source=BROKER_SOURCE, broker_id=broker_id,
        price=price, remaining=remaining, meta={"reported_filled": str(filled)},
    )
    intent.status = "CONFIRMED"
    intent.response = {"broker_id": broker_id, "status": state, "source": BROKER_SOURCE}
    emit(db, intent, "ORDER", "ORDER_ACKNOWLEDGED", "Broker acknowledged order", order_id=order.id,
         context=intent.response)
    if intent.exchange == "DERIV":
        record_fill(
            db, order, key=broker_id, broker_id=broker_id, quantity=Decimal("1"),
            price=price, fee=fee, timestamp=utcnow(),
        )
    return order


async def dispatch(db: Session, bot: Bot, intent: OrderIntent) -> None:
    # Only a newly claimed intent may be dispatched by the worker. Other
    # states (in particular UNKNOWN) must never be rewritten or resubmitted.
    if intent.status != "SUBMITTING":
        return
    risk = db.get(RiskConfig, 1)
    config = db.get(StrategyConfig, bot.strategy)
    connection = db.get(ExchangeConnection, intent.exchange)
    age = (utcnow() - aware(intent.timestamp)).total_seconds()
    if (
        bot.mode != "BROKER_DEMO" or intent.mode != "BROKER_DEMO"
        or bot.desired_state != "RUNNING" or not bot.run_id
        or (bot.run_id, bot.exchange, bot.strategy, bot.instrument)
        != (intent.run_id, intent.exchange, intent.strategy, intent.instrument)
        or intent.exchange not in {"DERIV", "KALSHI"}
        or intent.risk.get("allowed") is not True
        or not risk or risk.kill_switch or risk.thresholds != intent.request.get("risk_thresholds")
        or not config or not config.enabled
        or not connection or not connection.enabled or not connection.ciphertext
        or connection.status != "CONNECTED"
        or not 0 <= age <= 20
    ):
        intent.status = "CANCELLED"
        emit(db, intent, "ORDER", "PRE_DISPATCH_CANCELLED", "Order no longer eligible", order_id=intent.id)
        return
    venue = client(connection)
    decision = Decision.from_dict(intent.request["decision"])
    try:
        if isinstance(venue, Deriv):
            result = await venue.submit_demo(
                deriv_symbol(bot.instrument), decision.side, decision.price,
                duration=decision.duration_seconds // 60, client_order_id=intent.id,
                min_payout_ratio=Decimal(str(risk.thresholds.get("min_payout_ratio", "1.40"))),
            )
        else:
            result = await venue.submit_demo(
                bot.instrument, decision.side, decision.price, decision.quantity,
                client_order_id=intent.id, post_only=decision.post_only,
            )
    except VenueError as error:
        intent.status = "UNKNOWN" if error.outcome_unknown else "REJECTED"
        intent.response = {"code": error.code, "outcome_unknown": error.outcome_unknown}
        emit(db, intent, "ERROR", error.code, "Demo submission was not confirmed",
             severity="ERROR", order_id=intent.id, context=intent.response)
        if error.outcome_unknown:
            bot.state = "RISK_BLOCKED"
            bot.desired_state = "PAUSED"
        return
    # A malformed response or interrupted transaction leaves the durable SUBMITTING
    # claim for recovery. The next worker cannot send that claim again.
    acknowledge(db, intent, result)


async def recover_claim(db: Session, bot: Bot, venue, intent: OrderIntent) -> None:
    if intent.status not in {"SUBMITTING", "UNKNOWN"}:
        return
    if intent.status == "SUBMITTING":
        intent.status = "UNKNOWN"
        bot.state = "RISK_BLOCKED"
        bot.desired_state = "PAUSED"
        emit(db, intent, "RISK", "SUBMISSION_UNKNOWN", "Prior submission requires broker reconciliation",
             severity="WARNING", order_id=intent.id)
    if isinstance(venue, Kalshi):
        result = await venue.find_demo_order(intent.id, intent.instrument)
        if result:
            acknowledge(db, intent, result)


async def reconcile_deriv(db: Session, order: Order, venue: Deriv) -> None:
    if order.status == "SETTLED":
        return
    if (
        order.mode != "BROKER_DEMO" or order.exchange != "DERIV"
        or order.source != BROKER_SOURCE or order.currency != "USD"
        or not order.broker_id
    ):
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    result = await venue.reconcile_demo(order.broker_id)
    if (
        not isinstance(result, dict) or result.get("broker_id") != order.broker_id
        or result.get("source") != "DERIV_DEMO"
        or result.get("status") not in ("OPEN", "SETTLED")
    ):
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    if result.get("currency") != "USD":
        raise ValueError("UNSUPPORTED_CURRENCY")
    if confirmation_amount(result.get("price")) != order.price:
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    if result["status"] == "OPEN":
        return

    position = db.get(Position, order.id)
    if (
        position is None or position.status != "OPEN" or position.currency != "USD"
        or position.quantity != 1 or order.quantity != 1
        or position.exposure != order.price
    ):
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    payout = confirmation_amount(result.get("payout"))
    booked_fee = confirmation_amount(sum(db.scalars(select(Fee.amount).where(Fee.order_id == order.id)), ZERO))
    reported_fee = result.get("fee")
    # Do not invent or double-count a commission. A divergence requires a
    # separate verified accounting reconciliation before closing the position.
    if reported_fee is not None and confirmation_amount(reported_fee) != booked_fee:
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    profit = confirmation_amount(result.get("profit"), signed=True)
    gross = confirmation_amount(payout - position.exposure, signed=True)
    if profit != gross - booked_fee:
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    settled_at, observed_at = result.get("settled_at"), result.get("observed_at")
    if (
        type(settled_at) is not int or type(observed_at) is not int
        or not int(aware(order.timestamp).timestamp()) <= settled_at <= observed_at
    ):
        raise ValueError("INVALID_BROKER_SETTLEMENT")
    try:
        timestamp = moment(settled_at)
        moment(observed_at)
    except (OverflowError, OSError, ValueError):
        raise ValueError("INVALID_BROKER_SETTLEMENT") from None
    record_settlement(db, order, gross=gross, payout=payout, timestamp=timestamp)


def kalshi_timestamp(value, *, earliest: int):
    if type(value) is not int or value < earliest:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    try:
        return moment(value)
    except (OverflowError, OSError, ValueError):
        raise ValueError("INVALID_KALSHI_RECONCILIATION") from None


def kalshi_order_snapshot(order: Order, current: dict):
    if (
        not isinstance(current, dict)
        or current.get("source") != "KALSHI_DEMO"
        or current.get("broker_id") != order.broker_id
        or current.get("client_order_id") != order.intent_id
        or current.get("instrument") != order.instrument
        or current.get("side") != order.side
    ):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    state = current.get("status")
    quantity = confirmation_amount(current.get("quantity"))
    price = confirmation_amount(current.get("price"))
    filled = confirmation_amount(current.get("filled_quantity"))
    remaining = confirmation_amount(current.get("remaining_quantity"))
    fee = confirmation_amount(current.get("fee"), stored=False)
    previous_filled = confirmation_amount(order.data.get("reported_filled"))
    if (
        quantity != order.quantity or quantity <= ZERO or price != order.price
        or not ZERO < price < 1 or filled + remaining > quantity
        or filled < previous_filled or remaining > order.remaining_quantity
        or any(value != value.quantize(Decimal("0.01")) for value in (quantity, filled, remaining))
    ):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    if state == "OPEN":
        consistent = filled == ZERO and remaining == quantity
    elif state == "PARTIAL":
        consistent = ZERO < filled < quantity
    elif state == "FILLED":
        consistent = filled == quantity and remaining == ZERO
    elif state == "CANCELLED":
        consistent = filled == ZERO and remaining == ZERO
    else:
        consistent = False
    if not consistent:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    return state, filled, remaining, fee


def kalshi_fill_snapshot(db: Session, order: Order, rows: list, *, reported: Decimal, fee: Decimal):
    earliest = int(aware(order.timestamp).timestamp())
    normalized = {}
    for row in rows:
        if (
            not isinstance(row, dict) or row.get("broker_id") != order.broker_id
            or row.get("instrument") != order.instrument or row.get("side") != order.side
        ):
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
        key = row.get("fill_id")
        if not isinstance(key, str) or not key or key != key.strip() or len(key) > 100 or key in normalized:
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
        quantity = confirmation_amount(row.get("quantity"))
        price = confirmation_amount(row.get("price"))
        cost = confirmation_amount(row.get("fee"))
        timestamp = kalshi_timestamp(row.get("observed_at"), earliest=earliest)
        if not ZERO < quantity <= order.quantity or quantity != quantity.quantize(Decimal("0.01")):
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
        if not ZERO < price <= order.price:
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
        confirmation_amount(quantity * price)
        normalized[key] = (quantity, price, cost, timestamp)

    total = confirmation_amount(sum((row[0] for row in normalized.values()), ZERO))
    exposure = confirmation_amount(sum((row[0] * row[1] for row in normalized.values()), ZERO))
    costs = confirmation_amount(sum((row[2] for row in normalized.values()), ZERO))
    # Separate reads may temporarily disagree. Never release reserved exposure
    # or book a partial snapshot as complete; the worker must reconcile again
    # before it can open another order.
    if total > reported or costs > fee or (total == reported and costs != fee):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    if total < reported:
        raise VenueError("KALSHI_RECONCILIATION_INCOMPLETE", retryable=True)

    existing = db.scalars(select(Fill).where(Fill.order_id == order.id)).all()
    booked = {}
    for fill in existing:
        key = fill.broker_id
        if (
            key not in normalized or key in booked or fill.id != digest(order.id, key)
            or fill.mode != order.mode or fill.exchange != order.exchange
            or fill.instrument != order.instrument or fill.currency != "USD"
            or fill.source != order.source
            or normalized[key] != (fill.quantity, fill.price, fill.fee, aware(fill.timestamp))
        ):
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
        booked[key] = fill
    booked_fees = {row.id: row.amount for row in db.scalars(select(Fee).where(Fee.order_id == order.id))}
    if booked_fees != {row.id: row.fee for row in existing if row.fee > ZERO}:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    position = db.get(Position, order.id)
    if existing:
        if (
            position is None or position.status != "OPEN" or position.side != order.side
            or position.currency != "USD" or position.source != order.source
            or position.quantity != sum((row.quantity for row in existing), ZERO)
            or position.exposure != sum((row.quantity * row.price for row in existing), ZERO)
        ):
            raise ValueError("INVALID_KALSHI_RECONCILIATION")
    elif position is not None:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")

    # A broker fill identifier must not be attributed to two local orders.
    if normalized and db.scalar(select(Fill.id).where(
        Fill.mode == order.mode, Fill.exchange == order.exchange,
        Fill.broker_id.in_(list(normalized)), Fill.order_id != order.id,
    ).limit(1)) is not None:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    latest = max((int(row[3].timestamp()) for row in normalized.values()), default=earliest)
    return {key: row for key, row in normalized.items() if key not in booked}, total, exposure, costs, latest


def kalshi_resolution(order: Order, rows: list, *, quantity: Decimal, exposure: Decimal, fees: Decimal, latest: int):
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    matching = [row for row in rows if row.get("instrument") == order.instrument]
    if not matching:
        return None
    if len(matching) != 1:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    row = matching[0]
    event = order.data.get("event")
    if event and row.get("event_ticker") != event:
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    result = row.get("result")
    binary = {"YES": Decimal("1"), "NO": ZERO}
    if result not in ("YES", "NO", "SCALAR"):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    yes_payout = confirmation_amount(row.get("yes_payout", binary.get(result)))
    if yes_payout > 1 or (result in binary and yes_payout != binary[result]):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    per_contract = yes_payout if order.side == "YES" else Decimal("1") - yes_payout
    payout = confirmation_amount(quantity * per_contract)
    gross = confirmation_amount(payout - exposure, signed=True)
    confirmation_amount(gross - fees, signed=True)
    timestamp = kalshi_timestamp(row.get("observed_at"), earliest=latest)
    return gross, payout, timestamp


async def reconcile_kalshi(db: Session, order: Order, venue: Kalshi, *, cancel: bool) -> None:
    if order.status == "SETTLED":
        return
    if (
        order.mode != "BROKER_DEMO" or order.exchange != "KALSHI"
        or order.source != BROKER_SOURCE or order.currency != "USD"
        or order.side not in {"YES", "NO"} or not order.broker_id
    ):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    if cancel and order.remaining_quantity > ZERO:
        try:
            await venue.cancel_demo(order.broker_id, order.instrument)
        except VenueError as error:
            # Always reconcile after a lost cancellation acknowledgement.
            emit(db, order, "API", error.code, "Demo cancellation requires reconciliation",
                 severity="WARNING", order_id=order.id)
    result = await venue.reconcile_demo(order.broker_id, order.instrument)
    if (
        not isinstance(result, dict) or not isinstance(result.get("fills"), list)
        or not isinstance(result.get("settlements"), list)
    ):
        raise ValueError("INVALID_KALSHI_RECONCILIATION")
    state, reported, remaining, fee = kalshi_order_snapshot(order, result.get("order"))
    new_fills, total, exposure, costs, latest = kalshi_fill_snapshot(
        db, order, result["fills"], reported=reported, fee=fee,
    )
    settlement = None
    if total > ZERO and total == reported and remaining == ZERO:
        settlement = kalshi_resolution(
            order, result["settlements"], quantity=total, exposure=exposure, fees=costs, latest=latest,
        )

    persisted = order.status
    order.status = state
    order.remaining_quantity = remaining
    order.data = {**order.data, "reported_filled": str(reported)}
    for key, (quantity, price, cost, timestamp) in new_fills.items():
        record_fill(db, order, key=key, broker_id=key, quantity=quantity, price=price, fee=cost, timestamp=timestamp)
    db.flush()
    if order.status != persisted:
        emit(db, order, "ORDER", "ORDER_RECONCILED", "Broker order state updated",
             order_id=order.id, context={"status": order.status})
    if settlement is not None:
        gross, payout, timestamp = settlement
        record_settlement(db, order, gross=gross, payout=payout, timestamp=timestamp)

async def reconcile(db: Session, bot: Bot, venue, *, cancel: bool) -> None:
    pending = db.scalars(select(OrderIntent).where(
        OrderIntent.run_id == bot.run_id, OrderIntent.status.in_(("UNKNOWN", "SUBMITTING")),
    )).all()
    for intent in pending:
        await recover_claim(db, bot, venue, intent)
    orders = db.scalars(select(Order).where(
        Order.run_id == bot.run_id, Order.broker_id.is_not(None), Order.status != "SETTLED",
    )).all()
    for order in orders:
        if isinstance(venue, Deriv):
            await reconcile_deriv(db, order, venue)
        elif (order.status not in {"CANCELLED", "EXPIRED", "REJECTED"}
              or amount(order.data.get("reported_filled")) > ZERO):
            await reconcile_kalshi(db, order, venue, cancel=cancel)
