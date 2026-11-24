"""Recoverable execution worker for CAU-MVP-001."""

import asyncio
import logging
import signal
from dataclasses import replace

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from .config import LOG_RETENTION_DAYS
from .db import SessionLocal, engine, utcnow
from .demo_execution import client, dispatch, has_unmanaged_exposure, reconcile, verified_demo_balance
from .domain import check_risk, evaluate, validate_parameters
from .execution import (
    check_context,
    emit,
    execute_simulation,
    execution_inventory,
    propose,
    record_decision,
    risk_violation,
    save_balance,
    save_market,
    settle_simulation,
)
from .integrations import Deriv, VenueError
from .market_data import MarketTicker, deriv_symbol, epoch, fixture_enabled, observe
from .models import Bot, ExchangeConnection, OrderIntent, Position, RiskConfig, StrategyConfig, StrategyRun

LOG = logging.getLogger("cauche.worker")
FIXTURE_TICK = 0.08
MARKET_TICK = 5.0
LEASE_KEY = 0x434155434845


def locked_bot(db) -> Bot | None:
    return db.scalar(select(Bot).where(Bot.id == 1).with_for_update())


def set_state(db, bot: Bot, state: str) -> None:
    if bot.state == state:
        return
    bot.state = state
    bot.updated_at = utcnow()
    run = db.get(StrategyRun, bot.run_id) if bot.run_id else None
    if run:
        run.state = state
        if state == "STOPPED":
            run.finished_at = utcnow()
    emit(db, bot, "SYSTEM", f"BOT_{state}", f"Execution state changed to {state}")


def cancel_pending(db, bot: Bot) -> None:
    pending = db.scalars(select(OrderIntent).where(
        OrderIntent.run_id == bot.run_id, OrderIntent.status == "PROPOSED",
    ))
    for intent in pending:
        intent.status = "CANCELLED"
        emit(db, intent, "ORDER", "ORDER_CANCELLED", "Unsubmitted intent cancelled", order_id=intent.id)


def expire_replay(db, bot: Bot) -> None:
    cancel_pending(db, bot)
    positions = db.scalars(select(Position).where(
        Position.run_id == bot.run_id, Position.status == "OPEN",
    )).all()
    if positions:
        for position in positions:
            position.status = "UNRESOLVED"
        emit(db, bot, "SETTLEMENT", "FIXTURE_ENDED_WITH_OPEN_POSITIONS",
             "The fixture ended before observed settlement", severity="WARNING",
             context={"positions": len(positions)})
    bot.desired_state = "STOPPED"
    set_state(db, bot, "STOPPED")


def update_state(db, bot: Bot, *, blocked: bool) -> bool:
    if bot.desired_state == "STOPPED":
        cancel_pending(db, bot)
        set_state(db, bot, "STOPPED")
        return False
    if blocked:
        cancel_pending(db, bot)
        if bot.state not in {"RISK_BLOCKED", "ERROR"}:
            set_state(db, bot, "RISK_BLOCKED")
        bot.desired_state = "PAUSED"
        return False
    if bot.desired_state == "PAUSED":
        cancel_pending(db, bot)
        if bot.state not in {"RISK_BLOCKED", "ERROR"}:
            set_state(db, bot, "PAUSED")
        return False
    return bot.desired_state == "RUNNING"


def has_ambiguous_claim(db, bot: Bot) -> bool:
    if bot.mode != "BROKER_DEMO":
        return False
    return db.scalar(select(OrderIntent.id).where(
        OrderIntent.mode == "BROKER_DEMO",
        OrderIntent.status.in_(("SUBMITTING", "UNKNOWN")),
    ).limit(1)) is not None


def persist_recovery_barrier(db, bot: Bot) -> None:
    """Commit ambiguous claims and the pause before any broker lookup.

    Caller holds the bot row lock and the worker lease. A crash between
    claim persistence and submission is deliberately treated as ambiguous.
    """
    if bot.mode != "BROKER_DEMO":
        return
    pending = db.scalars(select(OrderIntent).where(
        OrderIntent.mode == "BROKER_DEMO",
        OrderIntent.status.in_(("SUBMITTING", "UNKNOWN")),
    ).order_by(OrderIntent.id).with_for_update()).all()
    for intent in pending:
        if intent.status == "SUBMITTING":
            intent.status = "UNKNOWN"
            intent.response = {"code": "SUBMISSION_UNKNOWN", "outcome_unknown": True}
            emit(db, intent, "RISK", "SUBMISSION_UNKNOWN", "Prior submission requires broker reconciliation",
                 severity="WARNING", order_id=intent.id)
    if pending and bot.desired_state != "STOPPED":
        update_state(db, bot, blocked=True)


def execution_scope(row) -> tuple:
    return row.run_id, row.mode, row.exchange, row.strategy, row.instrument


def worker_context(db, bot: Bot):
    run = db.get(StrategyRun, bot.run_id)
    config = db.get(StrategyConfig, bot.strategy)
    risk = db.get(RiskConfig, 1)
    if (
        run is None or config is None or risk is None
        or execution_scope(run) != execution_scope(bot)
        or bot.exchange not in {"DERIV", "KALSHI"}
        or bot.mode not in {"REPLAY", "PAPER", "BROKER_DEMO"}
        or bot.desired_state not in {"RUNNING", "PAUSED", "STOPPED"}
    ):
        raise ValueError("INVALID_WORKER_CONTEXT")
    return run, config, risk


def error_state(run_id: str | None, code: str, *, retryable: bool = True) -> None:
    with SessionLocal.begin() as db:
        bot = locked_bot(db)
        if not bot or bot.run_id != run_id:
            return
        bot.heartbeat_at = utcnow()
        bot.last_error = code
        if bot.desired_state == "RUNNING":
            set_state(db, bot, "DISCONNECTED" if retryable else "ERROR")
            if not retryable:
                bot.desired_state = "PAUSED"
        emit(db, bot, "ERROR", code, "The worker could not complete the operation", severity="ERROR")


_RETAIN_COUNTER = 0


async def step(ticker: MarketTicker | None = None) -> float:
    global _RETAIN_COUNTER
    run_id = None
    try:
        # Separate commit: a failed lookup or accounting transaction must
        # never roll back the persisted UNKNOWN state or safety pause.
        with SessionLocal.begin() as db:
            bot = locked_bot(db)
            if bot is None:
                return MARKET_TICK
            run_id = bot.run_id
            persist_recovery_barrier(db, bot)

        with SessionLocal.begin() as db:
            bot = locked_bot(db)
            if bot is None:
                return MARKET_TICK
            bot.heartbeat_at = utcnow()
            if _RETAIN_COUNTER == 0:
                purge_logs(db)
            _RETAIN_COUNTER = (_RETAIN_COUNTER + 1) % 720
            run_id = bot.run_id
            if not run_id:
                return 1.0
            run, config, risk = worker_context(db, bot)
            observed_scope = execution_scope(bot)
            limits = {**risk.thresholds, "kill_switch": risk.kill_switch}
            can_open = update_state(
                db, bot, blocked=risk.kill_switch or not config.enabled or has_ambiguous_claim(db, bot),
            )
            fixture = fixture_enabled(bot.mode, bot.exchange, bot.instrument)
            mode, exchange, instrument, cursor = bot.mode, bot.exchange, bot.instrument, bot.cursor
            strategy, version = run.strategy, run.strategy_version
            parameters = validate_parameters(strategy, run.parameters)
            connection = db.get(ExchangeConnection, exchange) if mode == "BROKER_DEMO" else None
            connection_identity = None
            if connection:
                if not connection.enabled or not connection.ciphertext or connection.status != "CONNECTED":
                    raise ValueError("DEMO_NOT_CONFIGURED")
                connection_identity = connection.ciphertext
                venue = client(connection)
                await reconcile(db, bot, venue, cancel=not can_open)
                can_open = can_open and bot.desired_state == "RUNNING"
            elif mode == "BROKER_DEMO":
                raise ValueError("DEMO_NOT_CONFIGURED")
            if ticker:
                stream_venue = None
                stream_instrument = instrument
                if can_open and not fixture:
                    if exchange == "DERIV":
                        stream_venue = Deriv()
                        stream_instrument = deriv_symbol(instrument)
                    elif connection:
                        stream_venue = venue
                await ticker.select(run_id if stream_venue else None, stream_venue, stream_instrument)
            if not can_open:
                if fixture or mode == "BROKER_DEMO" or not db.scalar(select(Position.id).where(
                    Position.run_id == run_id, Position.status == "OPEN",
                ).limit(1)):
                    if fixture and bot.desired_state == "STOPPED":
                        expire_replay(db, bot)
                    return 1.0 if fixture else MARKET_TICK

        # Source requests happen outside the state transaction. Re-check every
        # setting that can change while the observations are being obtained.
        market = await observe(mode, exchange, instrument, cursor, parameters, strategy=strategy)
        broker_balance = None
        if mode == "BROKER_DEMO":
            broker_balance = verified_demo_balance(exchange, await venue.verify_demo())
            if cursor == 0 and await has_unmanaged_exposure(venue):
                error_state(run_id, "UNMANAGED_DEMO_EXPOSURE", retryable=False)
                return MARKET_TICK

        claimed = None
        with SessionLocal.begin() as db:
            bot = locked_bot(db)
            if not bot or bot.run_id != run_id or bot.cursor != cursor:
                return MARKET_TICK
            run, config, risk = worker_context(db, bot)
            if (
                execution_scope(bot) != observed_scope or run.strategy_version != version
                or validate_parameters(run.strategy, run.parameters) != parameters
            ):
                raise ValueError("INVALID_WORKER_CONTEXT")
            if mode == "BROKER_DEMO":
                connection = db.get(ExchangeConnection, exchange)
                if (
                    not connection or not connection.enabled or connection.status != "CONNECTED"
                    or connection.ciphertext != connection_identity
                ):
                    raise ValueError("DEMO_NOT_CONFIGURED")
            limits = {**risk.thresholds, "kill_switch": risk.kill_switch}
            can_open = update_state(
                db, bot, blocked=risk.kill_switch or not config.enabled or has_ambiguous_claim(db, bot),
            )
            if fixture and not can_open:
                return 1.0
            if market is None:
                if not fixture:
                    raise ValueError("STALE_MARKET_DATA")
                expire_replay(db, bot)
                return 1.0
            save_market(db, bot, market, tick=ticker.latest(run_id) if ticker else None)
            if fixture or cursor == 0 or cursor % 6 == 0:
                emit(db, bot, "MARKET", "MARKET_OBSERVED", "Market observation persisted",
                     context={"source": market.source, "time": market.timestamp})
            if can_open and bot.mode == "BROKER_DEMO":
                connection = db.get(ExchangeConnection, exchange)
                connection.last_sync = utcnow()
            if bot.mode != "BROKER_DEMO":
                execute_simulation(db, bot, market, limits, can_open=can_open)
                db.flush()
                settle_simulation(db, bot, market)
            db.flush()
            available = save_balance(db, bot, market, broker_balance=broker_balance)
            bot.cursor += 1
            bot.last_error = None
            if not can_open or available is None:
                return FIXTURE_TICK if fixture else MARKET_TICK
            set_state(db, bot, "RUNNING")
            if bot.strategy == "KALSHI-STRAT-02":
                inventory, resting = execution_inventory(db, bot, market)
                market = replace(market, inventory=inventory, adverse=market.adverse or resting)
            decision = evaluate(bot.strategy, market, parameters)
            recorded = record_decision(db, bot, market, decision, version)
            if recorded and decision.orderable and not (fixture and market.outcome is not None):
                state = check_context(db, bot, market, available)
                assessment = check_risk(decision, market, limits, state, now=market.timestamp if fixture else epoch())
                if assessment.allowed:
                    intent = propose(db, bot, market, recorded, assessment, decision)
                    if bot.mode == "BROKER_DEMO":
                        intent.request = {**intent.request, "risk_thresholds": risk.thresholds}
                        intent.status = "SUBMITTING"
                        claimed = intent.id
                else:
                    risk_violation(db, bot, assessment.code, recorded.id, assessment.as_dict())
        if claimed:
            # Claim is durable before the write. Bot row serializes critical
            # controls through the bounded network operation.
            with SessionLocal.begin() as db:
                bot = locked_bot(db)
                intent = db.get(OrderIntent, claimed)
                if bot and intent and bot.run_id == run_id and intent.status == "SUBMITTING":
                    await dispatch(db, bot, intent)
        return FIXTURE_TICK if fixture else MARKET_TICK
    except VenueError as error:
        error_state(run_id, error.code, retryable=error.retryable)
        return MARKET_TICK
    except ValueError as error:
        if str(error) == "STALE_MARKET_DATA":
            error_state(run_id, "STALE_MARKET_DATA")
        else:
            error_state(run_id, "INVALID_CONFIGURATION_OR_SOURCE", retryable=False)
        return MARKET_TICK
    except KeyError:
        error_state(run_id, "INVALID_CONFIGURATION_OR_SOURCE", retryable=False)
        return MARKET_TICK


def purge_logs(db) -> None:
    from datetime import timedelta

    from .models import SystemEvent

    db.execute(delete(SystemEvent).where(
        SystemEvent.timestamp < utcnow() - timedelta(days=LOG_RETENTION_DAYS),
    ))


async def leased_step(ticker: MarketTicker | None = None) -> float:
    """Serialize one worker tick with a transaction-scoped PostgreSQL lease.

    Commit, rollback or connection termination releases the lock, including
    cancellation and exceptions. Never return a session-level lock to the pool.
    SQLite remains a single-worker local test backend, not a distributed lease.
    """
    with engine.begin() as lease:
        if lease.dialect.name == "postgresql" and not lease.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LEASE_KEY},
        ):
            return MARKET_TICK
        return await step(ticker)


async def loop() -> None:
    stopped = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            event_loop.add_signal_handler(signum, stopped.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: event_loop.call_soon_threadsafe(stopped.set))
    ticker = MarketTicker()
    LOG.info("worker_start")
    try:
        while not stopped.is_set():
            delay = MARKET_TICK
            try:
                delay = await leased_step(ticker)
            except SQLAlchemyError:
                LOG.warning("worker_database_unavailable")
            except Exception:
                # Upstream messages may contain secrets. Expose a safe code only.
                LOG.error("worker_unexpected_error")
            try:
                await asyncio.wait_for(stopped.wait(), timeout=delay)
            except TimeoutError:
                continue
    finally:
        await ticker.select(None)
        LOG.info("worker_stop")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(loop())


if __name__ == "__main__":
    main()
