"""Demo-only broker dispatch and reconciliation against official outcomes."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .domain import Decision
from .execution import amount, emit, moment, new_order, record_fill, record_settlement
from .integrations import Deriv, Kalshi, VenueError
from .market_data import deriv_symbol
from .models import Bot, ExchangeConnection, Order, OrderIntent, Position, RiskConfig, StrategyConfig
from .security import decrypt_credentials
from .serialization import jsonable

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


def acknowledge(db: Session, intent: OrderIntent, result: dict) -> Order:
    broker_id = str(result["broker_id"])
    if not broker_id or len(broker_id) > 100:
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    decision = Decision.from_dict(intent.request["decision"])
    broker_status = str(result["status"])
    state = broker_status.upper().replace("CANCELED", "CANCELLED")
    if state not in {"ACCEPTED", "OPEN", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
        state = "ACCEPTED"
    price = amount(result.get("price"), decision.price)
    remaining = amount(result.get("remaining_quantity"))
    if price <= ZERO or remaining < ZERO or remaining > decision.quantity:
        raise ValueError("INVALID_BROKER_CONFIRMATION")
    if intent.exchange == "DERIV" and result.get("currency") != "USD":
        raise ValueError("UNSUPPORTED_CURRENCY")
    observed_at = result.get("observed_at")
    if intent.exchange == "DERIV" and (type(observed_at) is not int or observed_at <= 0):
        raise ValueError("INVALID_BROKER_CONFIRMATION_TIME")
    order = new_order(
        db, intent, decision, status=state, source=BROKER_SOURCE, broker_id=broker_id,
        price=price, remaining=remaining, meta=jsonable({
            "reported_filled": amount(result.get("filled_quantity")),
            "broker_observed_at": observed_at,
            "broker_quoted_payout": result.get("payout"),
        }),
    )
    intent.status = "CONFIRMED"
    intent.response = {
        **intent.response, "broker_id": broker_id, "status": state, "source": BROKER_SOURCE,
        "acknowledged_at": jsonable(utcnow()), "broker_observed_at": observed_at,
    }
    emit(db, intent, "ORDER", "ORDER_ACKNOWLEDGED", "Broker acknowledged order", order_id=order.id,
         context=intent.response)
    if intent.exchange == "DERIV":
        record_fill(
            db, order, key=broker_id, broker_id=broker_id, quantity=Decimal("1"),
            price=price, fee=amount(result.get("fee")), timestamp=moment(observed_at),
        )
    return order


async def dispatch(db: Session, bot: Bot, intent: OrderIntent) -> None:
    risk = db.get(RiskConfig, 1)
    config = db.get(StrategyConfig, bot.strategy)
    connection = db.get(ExchangeConnection, intent.exchange)
    if (
        bot.mode != "BROKER_DEMO" or bot.desired_state != "RUNNING"
        or intent.risk.get("allowed") is not True
        or amount((intent.request.get("expectancy") or {}).get("conservative_net_ev")) <= ZERO
        or not risk or risk.kill_switch or risk.thresholds != intent.request.get("risk_thresholds")
        or not config or not config.enabled
        or not connection or connection.status != "CONNECTED"
        or utcnow().timestamp() - intent.timestamp.timestamp() > 20
    ):
        intent.status = "CANCELLED"
        emit(db, intent, "ORDER", "PRE_DISPATCH_CANCELLED", "Order no longer eligible", order_id=intent.id)
        return
    venue = client(connection)
    decision = Decision.from_dict(intent.request["decision"])
    intent.response = {"submitted_at": jsonable(utcnow()), "source": BROKER_SOURCE}
    try:
        if isinstance(venue, Deriv):
            result = await venue.submit_demo(
                deriv_symbol(bot.instrument), decision.side, decision.price,
                duration=decision.duration_seconds // 60, client_order_id=intent.id,
                min_payout_ratio=max(
                    amount(risk.thresholds.get("min_payout_ratio"), "1.40"),
                    decision.payout / decision.exposure,
                ),
            )
        else:
            result = await venue.submit_demo(
                bot.instrument, decision.side, decision.price, decision.quantity,
                client_order_id=intent.id, post_only=decision.post_only,
            )
    except VenueError as error:
        intent.status = "UNKNOWN" if error.outcome_unknown else "REJECTED"
        intent.response = {
            **intent.response, "code": error.code,
            "outcome_unknown": error.outcome_unknown, "failed_at": jsonable(utcnow()),
        }
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
    result = await venue.reconcile_demo(order.broker_id)
    if result["currency"] != "USD":
        raise ValueError("UNSUPPORTED_CURRENCY")
    if result["status"] != "SETTLED":
        return
    profit = amount(result["profit"])
    # Deriv settles the contract in one broker-reported net profit. Any explicit
    # commission returned by the adapter must already be reflected in that total.
    record_settlement(
        db, order, gross=profit + amount(result.get("fee")), payout=amount(result["payout"]),
        timestamp=moment(int(result.get("settled_at") or result["observed_at"])),
    )


async def reconcile_kalshi(db: Session, order: Order, venue: Kalshi, *, cancel: bool) -> None:
    if cancel and order.remaining_quantity > ZERO:
        try:
            await venue.cancel_demo(order.broker_id, order.instrument)
        except VenueError as error:
            # Always reconcile after a lost cancellation acknowledgement.
            emit(db, order, "API", error.code, "Demo cancellation requires reconciliation",
                 severity="WARNING", order_id=order.id)
    result = await venue.reconcile_demo(order.broker_id, order.instrument)
    current = result["order"]
    persisted = order.status
    if current:
        state = current["status"].upper().replace("CANCELED", "CANCELLED")
        if state in {"OPEN", "ACCEPTED", "PARTIAL", "FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
            order.status = state
            order.remaining_quantity = amount(current.get("remaining_quantity"))
            order.data = {**order.data, "reported_filled": str(current.get("filled_quantity") or "0")}
    for fill in result["fills"]:
        if (
            fill["broker_id"] != order.broker_id or fill["instrument"] != order.instrument
            or fill["side"] != order.side
        ):
            continue
        record_fill(
            db, order, key=fill["fill_id"], broker_id=fill["fill_id"],
            quantity=amount(fill["quantity"]), price=amount(fill["price"]), fee=amount(fill["fee"]),
            timestamp=moment(int(fill["observed_at"])),
        )
    db.flush()
    if order.status != persisted:
        emit(db, order, "ORDER", "ORDER_RECONCILED", "Broker order state updated",
             order_id=order.id, context={"status": order.status})
    position = db.get(Position, order.id)
    reported = amount(order.data.get("reported_filled"))
    if not position or position.quantity != reported or order.remaining_quantity > ZERO:
        return
    for resolution in result["settlements"]:
        if resolution["instrument"] != order.instrument:
            continue
        yes_payout = resolution.get("yes_payout")
        if yes_payout is None:
            yes_payout = {"YES": Decimal("1"), "NO": ZERO}.get(resolution["result"])
        if yes_payout is None:
            continue
        per_contract = amount(yes_payout) if order.side == "YES" else Decimal("1") - amount(yes_payout)
        if not ZERO <= per_contract <= Decimal("1"):
            continue
        payout = position.quantity * per_contract
        record_settlement(
            db, order, gross=payout - position.exposure, payout=payout,
            timestamp=moment(int(resolution["observed_at"])),
        )
        return


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
