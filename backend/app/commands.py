"""Transactional strategy, execution and broker connection commands."""


from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .events import audit
from .models import Bot, ExchangeConnection, Order, OrderIntent, Position, RiskConfig
from .models import StrategyConfig, StrategyRun
from .queries import FIXTURES, OPEN_ORDERS, OPEN_POSITIONS
from .schemas import Launch
from .security import decrypt_credentials
from .serialization import jsonable, record

INFLIGHT_INTENTS = ("PROPOSED", "PENDING", "SUBMITTING", "UNKNOWN")
STABLE_START = ("STOPPED", "ERROR")


def bot_row(session: Session, *, locked=False) -> Bot:
    statement = select(Bot).where(Bot.id == 1)
    if locked:
        statement = statement.with_for_update()
    bot = session.scalar(statement)
    if bot is None:
        raise HTTPException(503, "MIGRATE_DATABASE")
    return bot


def risk_row(session: Session) -> RiskConfig:
    row = session.get(RiskConfig, 1)
    if row is None:
        raise HTTPException(503, "MIGRATE_DATABASE")
    return row


def risk_payload(row: RiskConfig) -> dict:
    from .domain import DEFAULT_LIMITS

    defaults = {key: value for key, value in DEFAULT_LIMITS.items() if key != "kill_switch"}
    return jsonable({"kill_switch": row.kill_switch, "thresholds": {**defaults, **row.thresholds}})


def strategy_payload(row: StrategyConfig, definition: dict) -> dict:
    return jsonable({
        **definition,
        "version": row.version,
        "enabled": row.enabled,
        "parameters": {**definition.get("defaults", {}), **row.parameters},
    })


def pending_exposure(session: Session, mode: str, exchange: str | None = None) -> bool:
    intent = select(OrderIntent.id).where(OrderIntent.status.in_(INFLIGHT_INTENTS))
    order = select(Order.id).where(Order.status.in_(OPEN_ORDERS))
    position = select(Position.id).where(Position.status.in_(OPEN_POSITIONS))
    if mode == "PAPER":
        # Unsettled simulated fixture positions belong to completed finite replays.
        # Retain them in history, but don't block the next independent fixture run.
        position = position.where(~Position.instrument.startswith("FIXTURE"))
        order = order.where(~Order.instrument.startswith("FIXTURE"))
        intent = intent.where(~OrderIntent.instrument.startswith("FIXTURE"))
    for model, statement in ((OrderIntent, intent), (Order, order), (Position, position)):
        statement = statement.where(model.mode == mode)
        if exchange:
            statement = statement.where(model.exchange == exchange)
        if session.scalar(statement.limit(1)) is not None:
            return True
    return False


def validate_launch(session: Session, payload: Launch) -> StrategyConfig:
    from .domain import validate_parameters

    if payload.strategy.split("-")[0] != payload.exchange:
        raise HTTPException(422, "STRATEGY_EXCHANGE_MISMATCH")
    config = session.get(StrategyConfig, payload.strategy)
    if config is None or not config.enabled:
        raise HTTPException(409, "STRATEGY_DISABLED")
    try:
        validate_parameters(config.id, config.parameters)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, "STRATEGY_CONFIGURATION_INVALID") from exc
    if risk_row(session).kill_switch:
        raise HTTPException(409, "KILL_SWITCH_ACTIVE")
    if pending_exposure(session, "BROKER_DEMO") or pending_exposure(session, "PAPER"):
        raise HTTPException(409, "UNRESOLVED_EXPOSURE")
    fixture = payload.instrument.startswith("FIXTURE")
    if payload.mode == "REPLAY" or (payload.mode == "PAPER" and fixture):
        if payload.instrument.removeprefix("FIXTURE:") not in FIXTURES[payload.exchange]:
            raise HTTPException(422, "FIXTURE_NOT_FOUND")
    if payload.mode == "BROKER_DEMO":
        if not payload.confirm:
            raise HTTPException(422, "CONFIRMATION_REQUIRED")
        if fixture:
            raise HTTPException(422, "FIXTURE_NOT_ALLOWED_IN_BROKER_DEMO")
        connection = session.get(ExchangeConnection, payload.exchange)
        if not connection or not connection.enabled or not connection.ciphertext:
            raise HTTPException(409, "BROKER_NOT_CONFIGURED")
        if connection.status != "CONNECTED":
            raise HTTPException(409, "BROKER_AUTHENTICATION_REQUIRED")
        try:
            decrypt_credentials(connection.ciphertext)
        except ValueError as exc:
            raise HTTPException(409, "CREDENTIALS_UNAVAILABLE") from exc
    return config


def start_bot(session: Session, payload: Launch, owner_id: int) -> dict:
    bot = bot_row(session, locked=True)
    if bot.state not in STABLE_START or bot.desired_state != "STOPPED":
        raise HTTPException(409, "INVALID_BOT_TRANSITION")
    config = validate_launch(session, payload)
    now = utcnow()
    run = StrategyRun(
        mode=payload.mode, exchange=payload.exchange, instrument=payload.instrument,
        strategy=payload.strategy, state="STARTING",
        strategy_version=config.version, parameters=config.parameters, timestamp=now,
    )
    session.add(run)
    session.flush()
    run.run_id = run.id
    bot.mode, bot.exchange = payload.mode, payload.exchange
    bot.strategy, bot.instrument = payload.strategy, payload.instrument
    bot.state, bot.desired_state = "STARTING", "RUNNING"
    bot.cursor, bot.run_id, bot.last_error = 0, run.id, None
    bot.updated_at = now
    audit(session, "ADMIN", "BOT_START", "Inicialização solicitada.", user_id=owner_id,
          context={"run_id": run.id, "mode": run.mode, "exchange": run.exchange,
                   "instrument": run.instrument, "strategy": run.strategy})
    session.commit()
    return record(bot)


def change_bot(session: Session, action: str, owner_id: int) -> dict:
    bot = bot_row(session, locked=True)
    if action == "pause" and bot.state == "RUNNING" and bot.desired_state == "RUNNING":
        bot.state, bot.desired_state = "PAUSING", "PAUSED"
    elif action == "resume" and bot.state == "PAUSED" and bot.desired_state == "PAUSED":
        if risk_row(session).kill_switch:
            raise HTTPException(409, "KILL_SWITCH_ACTIVE")
        config = session.get(StrategyConfig, bot.strategy)
        if not config or not config.enabled:
            raise HTTPException(409, "STRATEGY_DISABLED")
        bot.state, bot.desired_state = "STARTING", "RUNNING"
    elif action == "stop" and bot.state not in {"STOPPED", "STOPPING"}:
        bot.state, bot.desired_state = "STOPPING", "STOPPED"
    else:
        raise HTTPException(409, "INVALID_BOT_TRANSITION")
    bot.updated_at = utcnow()
    audit(session, "ADMIN", f"BOT_{action.upper()}", "Transição do motor solicitada.",
          user_id=owner_id, context={"run_id": bot.run_id, "state": bot.state})
    session.commit()
    return record(bot)


def query_connection(session: Session, exchange: str) -> ExchangeConnection:
    row = session.get(ExchangeConnection, exchange)
    if row is None:
        raise HTTPException(503, "MIGRATE_DATABASE")
    return row


def lock_connection_change(session: Session, exchange: str) -> ExchangeConnection:
    bot = bot_row(session, locked=True)
    if (bot.exchange == exchange and bot.mode == "BROKER_DEMO" and bot.state not in STABLE_START
            or pending_exposure(session, "BROKER_DEMO", exchange)):
        raise HTTPException(409, "BROKER_IN_USE")
    return query_connection(session, exchange)


def checked_credentials(exchange: str, credentials: dict[str, str]) -> dict:
    required = {"app_id", "token"} if exchange == "DERIV" else {"key_id", "private_key"}
    optional = {"account_id"} if exchange == "DERIV" else set()
    actual = set(credentials)
    if not required <= actual <= required | optional:
        raise HTTPException(422, "INVALID_CREDENTIAL_FIELDS")
    result = {key: value.strip() for key, value in credentials.items() if value.strip()}
    if not required <= set(result):
        raise HTTPException(422, "INVALID_CREDENTIAL_FIELDS")
    max_lengths = {"app_id": 100, "account_id": 100, "key_id": 120, "private_key": 10000}
    for key, value in result.items():
        if len(value) > max_lengths.get(key, 2048) or (key != "private_key" and not value.isascii()):
            raise HTTPException(422, "INVALID_CREDENTIAL_FIELDS")
    return result
