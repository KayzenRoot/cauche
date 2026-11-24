"""CD-04: real transaction boundaries, no broker network or credentials."""

import asyncio
from dataclasses import replace
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import demo_execution, worker
from app.db import Base, utcnow
from app.domain import Book, Candle, Decision, Market
from app.integrations import Deriv, Kalshi, VenueError
from app.quant.types import Forecast
from app.models import (
    BalanceSnapshot, Bot, ExchangeConnection, Fee, Fill, MarketSnapshot, Order, OrderIntent, Position,
    RiskConfig, RiskViolation, Settlement, Signal,
    StrategyConfig, StrategyRun, SystemEvent,
)

STRATEGY = "KALSHI-STRAT-01"
MARKET = "MOCK-MARKET"


@pytest.fixture(params=["sqlite", "postgresql"])
def recovery_db(request, tmp_path, monkeypatch):
    schema = None
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
        target = engine
    else:
        port = os.environ.get("CAUCHE_TEST_POSTGRES_PORT")
        if not port:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                pytest.fail("PostgreSQL service is required in CI")
            pytest.skip("Set CAUCHE_TEST_POSTGRES_PORT for the isolated test service")
        assert port.isdecimal() and 0 < int(port) < 65536
        password_file = os.environ.get("CAUCHE_TEST_POSTGRES_PASSWORD_FILE")
        password = (
            Path(password_file).read_text(encoding="utf-8").strip()
            if password_file else os.environ.get("CAUCHE_TEST_POSTGRES_PASSWORD", "")
        )
        if os.environ.get("GITHUB_ACTIONS") == "true" and not password:
            pytest.fail("PostgreSQL CI requires a generated password")
        credentials = f"cauche_test:{quote(password, safe='')}" if password else "cauche_test"
        # Fixed loopback host/test-only database, never DATABASE_URL or production.
        engine = create_engine(
            f"postgresql+psycopg://{credentials}@127.0.0.1:{int(port)}/cauche_test",
            connect_args={"connect_timeout": 5}, hide_parameters=True,
        )
        schema = "recovery_" + uuid4().hex
        with engine.begin() as db:
            db.execute(CreateSchema(schema))
        target = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(target)
        factory = sessionmaker(bind=target, expire_on_commit=False)
        monkeypatch.setattr(worker, "SessionLocal", factory)
        monkeypatch.setattr(worker, "_RETAIN_COUNTER", 1)
        with factory.begin() as db:
            db.add_all([
                Bot(mode="BROKER_DEMO", exchange="KALSHI", strategy=STRATEGY,
                    instrument=MARKET, state="RUNNING", desired_state="RUNNING", run_id="run-current"),
                RiskConfig(thresholds={}), StrategyConfig(id=STRATEGY),
                ExchangeConnection(exchange="KALSHI", enabled=True, status="CONNECTED", ciphertext="fixture"),
                StrategyRun(id="run-current", run_id="run-current", mode="BROKER_DEMO", exchange="KALSHI",
                            strategy=STRATEGY, instrument=MARKET, state="RUNNING", parameters={}),
            ])
        yield factory
    finally:
        if schema is not None:
            with engine.begin() as db:
                db.execute(DropSchema(schema, cascade=True))
        engine.dispose()


def seed_claim(factory, *, run_id="run-current", status="SUBMITTING"):
    decision = Decision(strategy=STRATEGY, version="1", reason="TEST", side="YES",
                        price=Decimal("0.40"), quantity=Decimal("1"), orderable=True,
                        exposure=Decimal("0.40"), instrument=MARKET)
    fields = dict(mode="BROKER_DEMO", exchange="KALSHI", strategy=STRATEGY,
                  instrument=MARKET, run_id=run_id, timestamp=utcnow())
    with factory.begin() as db:
        db.add(Signal(id="signal-fixture", direction="YES", reason="TEST", **fields))
        db.flush()
        db.add(OrderIntent(id="claim-fixture", signal_id="signal-fixture", status=status,
                           risk={"allowed": True},
                           request={"decision": decision.as_dict(), "risk_thresholds": {},
                                    "expectancy": {"conservative_net_ev": "0.10"}}, **fields))


def recovered_ack():
    return {
        "broker_id": "broker-fixture", "client_order_id": "claim-fixture",
        "source": "KALSHI_DEMO", "instrument": MARKET, "side": "YES",
        "status": "CANCELLED", "price": Decimal("0.40"), "quantity": Decimal("1"),
        "filled_quantity": Decimal("0"), "remaining_quantity": Decimal("0"), "fee": Decimal("0"),
    }


@pytest.fixture
def venue(monkeypatch):
    mock = Mock(spec=Kalshi)
    mock.find_demo_order = AsyncMock(return_value=None)
    mock.submit_demo = AsyncMock(return_value=recovered_ack())
    mock.reconcile_demo = AsyncMock(return_value={"order": None, "fills": [], "settlements": []})
    mock.cancel_demo = AsyncMock()
    monkeypatch.setattr(worker, "client", lambda _: mock)
    monkeypatch.setattr(demo_execution, "client", lambda _: mock)
    monkeypatch.setattr(worker, "observe", AsyncMock(side_effect=AssertionError("must not open new orders")))
    return mock


def assert_barrier(factory, *, event_count=1):
    with factory() as db:
        claim = db.get(OrderIntent, "claim-fixture")
        bot = db.get(Bot, 1)
        assert claim.status == "UNKNOWN"
        assert bot.state == "RISK_BLOCKED" and bot.desired_state == "PAUSED"
        assert db.get(StrategyRun, "run-current").state == "RISK_BLOCKED"
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(Fill)) == 0
        assert db.scalar(select(func.count()).select_from(SystemEvent).where(
            SystemEvent.code == "SUBMISSION_UNKNOWN",
        )) == event_count


@pytest.mark.parametrize("failure", ["timeout", "malformed", "missing"])
async def test_recovery_barrier_committed_before_failed_lookup(recovery_db, venue, failure):
    seed_claim(recovery_db)

    async def lookup(*_):
        # A different session must see committed state before remote work.
        assert_barrier(recovery_db)
        if failure == "timeout":
            raise VenueError("MOCK_TIMEOUT", retryable=True)
        if failure == "malformed":
            return {**recovered_ack(), "price": "NaN"}
        return None

    venue.find_demo_order.side_effect = lookup
    for _ in range(2):
        await worker.step()
        assert_barrier(recovery_db)
    assert venue.find_demo_order.await_count == 2
    venue.submit_demo.assert_not_awaited()


async def test_crash_after_ack_rolls_back_order_but_recovery_is_idempotent(recovery_db, venue):
    seed_claim(recovery_db)
    with pytest.raises(RuntimeError, match="simulated crash"):
        with recovery_db.begin() as db:
            await demo_execution.dispatch(db, db.get(Bot, 1), db.get(OrderIntent, "claim-fixture"))
            assert db.get(Order, "claim-fixture") is not None
            raise RuntimeError("simulated crash")
    with recovery_db() as db:
        assert db.get(OrderIntent, "claim-fixture").status == "SUBMITTING"
        assert db.get(Order, "claim-fixture") is None
    venue.find_demo_order.return_value = recovered_ack()
    for _ in range(3):
        await worker.step()
    with recovery_db() as db:
        claim = db.get(OrderIntent, "claim-fixture")
        assert claim.status == "CONFIRMED"
        assert db.get(Bot, 1).desired_state == "PAUSED"
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.get(Order, "claim-fixture").broker_id == "broker-fixture"
        assert db.scalar(select(func.count()).select_from(Fill)) == 0
        assert db.scalar(select(func.count()).select_from(SystemEvent).where(
            SystemEvent.code == "SUBMISSION_UNKNOWN",
        )) == 1
    venue.submit_demo.assert_awaited_once()
    venue.find_demo_order.assert_awaited_once_with("claim-fixture", MARKET)


async def test_historical_claim_blocks_new_run_without_wrong_lookup(recovery_db, venue):
    seed_claim(recovery_db, run_id="previous-run")
    await worker.step()
    assert_barrier(recovery_db)
    # Simulate an attempted resume: the persisted global guard must re-block it.
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        bot.state, bot.desired_state = "STARTING", "RUNNING"
    await worker.step()
    assert_barrier(recovery_db)
    venue.find_demo_order.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()


async def test_connection_failure_cannot_rollback_recovery_barrier(recovery_db, venue, monkeypatch):
    seed_claim(recovery_db)
    monkeypatch.setattr(worker, "client", Mock(side_effect=ValueError("DEMO_NOT_CONFIGURED")))
    await worker.step()
    assert_barrier(recovery_db)
    venue.find_demo_order.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()


async def test_already_confirmed_claim_is_not_recovered_again(recovery_db, venue):
    seed_claim(recovery_db, status="CONFIRMED")
    with recovery_db.begin() as db:
        await demo_execution.recover_claim(
            db, db.get(Bot, 1), venue, db.get(OrderIntent, "claim-fixture"),
        )
    venue.find_demo_order.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()


# CD-05: real ledger writes/rollback on both existing test databases.
@pytest.mark.parametrize("invalid", [False, True])
async def test_deriv_settlement_ledger_exact_and_idempotent(recovery_db, invalid):
    now = utcnow()
    decision = Decision(
        strategy="DERIV-STRAT-01", version="1", reason="TEST", side="CALL",
        price=Decimal("1"), quantity=Decimal("1"), exposure=Decimal("1"),
        orderable=True, instrument="EUR/USD",
    )
    fields = dict(mode="BROKER_DEMO", exchange="DERIV", strategy="DERIV-STRAT-01",
                  instrument="EUR/USD", run_id="run-deriv", timestamp=now)
    with recovery_db.begin() as db:
        db.add(Signal(id="deriv-signal", direction="CALL", reason="TEST", **fields))
        db.flush()
        intent = OrderIntent(id="deriv-claim", signal_id="deriv-signal", status="SUBMITTING",
                             request={"decision": decision.as_dict()}, **fields)
        db.add(intent)
        db.flush()
        demo_execution.acknowledge(db, intent, {
            "source": "DERIV_DEMO", "client_order_id": intent.id, "broker_id": "123",
            "status": "OPEN", "currency": "USD", "price": Decimal("1"),
            "quantity": Decimal("1"), "fee": Decimal("0.10"),
            "observed_at": int(now.timestamp()),
        })
    when = int(now.timestamp()) + 300
    result = dict(broker_id="123", source="DERIV_DEMO", currency="USD", status="SETTLED",
                  price=Decimal("1"), payout=Decimal("1.80"),
                  profit=Decimal("0.80" if invalid else "0.70"), fee=Decimal("0.10"),
                  settled_at=when, observed_at=when + 1)
    venue = Mock()
    venue.reconcile_demo = AsyncMock(return_value=result)
    if invalid:
        with pytest.raises(ValueError, match="INVALID_BROKER_SETTLEMENT"):
            with recovery_db.begin() as db:
                await demo_execution.reconcile_deriv(db, db.get(Order, "deriv-claim"), venue)
        with recovery_db() as db:
            assert db.get(Order, "deriv-claim").status == "OPEN"
            assert db.get(Position, "deriv-claim").status == "OPEN"
            assert db.get(Settlement, "deriv-claim") is None
    else:
        for _ in range(2):
            with recovery_db.begin() as db:
                await demo_execution.reconcile_deriv(db, db.get(Order, "deriv-claim"), venue)
        with recovery_db() as db:
            entry = db.get(Settlement, "deriv-claim")
            assert entry.gross_pnl == Decimal("0.80")
            assert entry.fees == Decimal("0.10")
            assert entry.net_pnl == Decimal("0.70")
            assert entry.payout == Decimal("1.80")
            assert db.get(Position, "deriv-claim").status == "SETTLED"
            assert db.get(Order, "deriv-claim").status == "SETTLED"
            assert db.scalar(select(func.count()).select_from(Settlement)) == 1
    with recovery_db() as db:
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.scalar(select(func.count()).select_from(Fee)) == 1
    venue.reconcile_demo.assert_awaited_once_with("123")


# CD-06: validate normalized Kalshi snapshots against the real local ledger.
def kalshi_snapshot(*, filled="0", remaining="1", state="OPEN", fee="0", fills=None, settlements=None):
    return {
        "order": {
            "source": "KALSHI_DEMO", "client_order_id": "claim-fixture",
            "broker_id": "broker-fixture", "instrument": MARKET, "side": "YES",
            "status": state, "quantity": Decimal("1"), "price": Decimal("0.40"),
            "filled_quantity": Decimal(filled), "remaining_quantity": Decimal(remaining),
            "fee": Decimal(fee),
        },
        "fills": fills or [], "settlements": settlements or [],
    }


def kalshi_fill(key, *, quantity="0.50", price="0.38", fee="0.01"):
    return {
        "fill_id": key, "broker_id": "broker-fixture", "instrument": MARKET, "side": "YES",
        "quantity": Decimal(quantity), "price": Decimal(price), "fee": Decimal(fee),
        "observed_at": int(utcnow().timestamp()) + 5,
    }


def kalshi_resolution(*, payout="1", result="YES"):
    return {"instrument": MARKET, "result": result, "yes_payout": Decimal(payout),
            "observed_at": int(utcnow().timestamp()) + 10}


def seed_kalshi_order(factory):
    seed_claim(factory)
    with factory.begin() as db:
        demo_execution.acknowledge(db, db.get(OrderIntent, "claim-fixture"), kalshi_snapshot()["order"])


async def apply_kalshi(factory, result, *, cancel=False, lost=False):
    venue = Mock(spec=Kalshi)
    venue.reconcile_demo = AsyncMock(return_value=result)
    venue.cancel_demo = AsyncMock(side_effect=VenueError("MOCK_CANCEL_LOST", outcome_unknown=True) if lost else None)
    venue.submit_demo = AsyncMock()
    with factory.begin() as db:
        await demo_execution.reconcile_kalshi(db, db.get(Order, "claim-fixture"), venue, cancel=cancel)
    venue.submit_demo.assert_not_awaited()
    return venue


async def test_kalshi_partial_cancel_and_settle_once(recovery_db):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    partial = kalshi_snapshot(filled="0.5", remaining="0.5", state="PARTIAL", fee="0.01", fills=[fill])
    for _ in range(2):
        await apply_kalshi(recovery_db, partial)
    with recovery_db() as db:
        assert db.get(Position, "claim-fixture").quantity == Decimal("0.5")
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.scalar(select(func.count()).select_from(Fee)) == 1
        assert db.get(Order, "claim-fixture").remaining_quantity == Decimal("0.5")
    closed = kalshi_snapshot(filled="0.5", remaining="0", state="PARTIAL", fee="0.01",
                             fills=[fill], settlements=[kalshi_resolution()])
    venue = await apply_kalshi(recovery_db, closed, cancel=True, lost=True)
    venue.cancel_demo.assert_awaited_once_with("broker-fixture", MARKET)
    venue.reconcile_demo.assert_awaited_once_with("broker-fixture", MARKET)
    again = await apply_kalshi(recovery_db, closed, cancel=True)
    again.reconcile_demo.assert_not_awaited()
    again.cancel_demo.assert_not_awaited()
    with recovery_db() as db:
        row = db.get(Settlement, "claim-fixture")
        assert row.payout == Decimal("0.5")
        assert row.gross_pnl == Decimal("0.31")
        assert row.fees == Decimal("0.01")
        assert row.net_pnl == Decimal("0.30")
        assert db.get(Order, "claim-fixture").status == "SETTLED"
        assert db.scalar(select(func.count()).select_from(Settlement)) == 1
        assert db.scalar(select(func.count()).select_from(Fill)) == 1


async def test_kalshi_waits_for_complete_fills_before_settling(recovery_db):
    seed_kalshi_order(recovery_db)
    first, second = kalshi_fill("fill-one"), kalshi_fill("fill-two", price="0.40")
    snapshot = kalshi_snapshot(filled="1", remaining="0", state="FILLED", fee="0.02",
                               fills=[first], settlements=[kalshi_resolution()])
    with pytest.raises(VenueError, match="KALSHI_RECONCILIATION_INCOMPLETE") as error:
        await apply_kalshi(recovery_db, snapshot)
    assert error.value.retryable
    with recovery_db() as db:
        assert db.get(Position, "claim-fixture") is None
        assert db.get(Order, "claim-fixture").status == "OPEN"
        assert db.get(Settlement, "claim-fixture") is None
    snapshot["fills"].append(second)
    await apply_kalshi(recovery_db, snapshot)
    with recovery_db() as db:
        row = db.get(Settlement, "claim-fixture")
        assert row.payout == Decimal("1")
        assert row.gross_pnl == Decimal("0.61")
        assert row.net_pnl == Decimal("0.59")
        assert db.scalar(select(func.count()).select_from(Fill)) == 2


async def test_kalshi_cancel_without_fills(recovery_db):
    seed_kalshi_order(recovery_db)
    snapshot = kalshi_snapshot(remaining="0", state="CANCELLED")
    venue = await apply_kalshi(recovery_db, snapshot, cancel=True)
    venue.cancel_demo.assert_awaited_once()
    with recovery_db() as db:
        assert db.get(Order, "claim-fixture").status == "CANCELLED"
        assert db.get(Position, "claim-fixture") is None
        assert db.get(Settlement, "claim-fixture") is None


@pytest.mark.parametrize("kind", [
    "wrong-order", "wrong-client", "wrong-side", "wrong-source", "wrong-price",
    "unknown-state", "quantity-overflow", "fractional-precision", "wrong-fill",
    "wrong-fill-side", "duplicate-in-page", "overfill", "negative-fee",
    "fee-mismatch", "overprice", "invalid-time", "invalid-payout", "conflicting-resolution", "premature-resolution",
])
async def test_kalshi_invalid_snapshot_rolls_back(recovery_db, kind):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    snapshot = kalshi_snapshot(filled="0.5", remaining="0", state="PARTIAL", fee="0.01",
                               fills=[fill], settlements=[kalshi_resolution()])
    current = snapshot["order"]
    if kind == "wrong-order":
        current["broker_id"] = "other"
    elif kind == "wrong-client":
        current["client_order_id"] = "other"
    elif kind == "wrong-side":
        current["side"] = "NO"
    elif kind == "wrong-source":
        current["source"] = "KALSHI_REAL"
    elif kind == "wrong-price":
        current["price"] = "0.39"
    elif kind == "unknown-state":
        current["status"] = "UNKNOWN"
    elif kind == "quantity-overflow":
        current["remaining_quantity"] = "0.6"
    elif kind == "fractional-precision":
        current["filled_quantity"] = "0.501"
    elif kind == "wrong-fill":
        fill["broker_id"] = "other"
    elif kind == "wrong-fill-side":
        fill["side"] = "NO"
    elif kind == "duplicate-in-page":
        snapshot["fills"].append(dict(fill))
    elif kind == "overfill":
        fill["quantity"] = "0.51"
    elif kind == "negative-fee":
        fill["fee"] = "-0.01"
    elif kind == "fee-mismatch":
        current["fee"] = "0.02"
    elif kind == "overprice":
        fill["price"] = "0.41"
    elif kind == "invalid-time":
        fill["observed_at"] = True
    elif kind == "invalid-payout":
        snapshot["settlements"][0]["yes_payout"] = "0.5"
    elif kind == "premature-resolution":
        snapshot["settlements"][0]["observed_at"] = fill["observed_at"] - 1
    else:
        snapshot["settlements"].append(kalshi_resolution(payout="0", result="NO"))
    with pytest.raises(ValueError, match="INVALID_(KALSHI_RECONCILIATION|BROKER_CONFIRMATION)"):
        await apply_kalshi(recovery_db, snapshot)
    with recovery_db() as db:
        order = db.get(Order, "claim-fixture")
        assert order.status == "OPEN" and order.remaining_quantity == Decimal("1")
        assert db.get(Position, order.id) is None
        assert db.scalar(select(func.count()).select_from(Fill)) == 0
        assert db.scalar(select(func.count()).select_from(Fee)) == 0
        assert db.scalar(select(func.count()).select_from(Settlement)) == 0


@pytest.mark.parametrize("kind", ["changed-fill", "missing-fill", "quantity-regression", "reopened-remainder", "fee-ledger"])
async def test_kalshi_existing_ledger_is_not_overwritten(recovery_db, kind):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    initial = kalshi_snapshot(filled="0.5", remaining="0", state="PARTIAL", fee="0.01", fills=[fill])
    await apply_kalshi(recovery_db, initial)
    if kind == "changed-fill":
        fill["price"] = "0.39"
    elif kind == "missing-fill":
        initial["fills"] = []
    elif kind == "quantity-regression":
        initial["order"]["filled_quantity"] = "0.4"
    elif kind == "reopened-remainder":
        initial["order"]["remaining_quantity"] = "0.5"
    else:
        with recovery_db.begin() as db:
            db.scalar(select(Fee)).amount = Decimal("0.02")
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        await apply_kalshi(recovery_db, initial)
    with recovery_db() as db:
        assert db.get(Order, "claim-fixture").status == "PARTIAL"
        assert db.get(Position, "claim-fixture").exposure == Decimal("0.19")
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.get(Settlement, "claim-fixture") is None


async def test_kalshi_filled_ack_blocks_new_run_before_position_exists(recovery_db):
    from app.commands import pending_exposure

    seed_claim(recovery_db)
    with recovery_db.begin() as db:
        demo_execution.acknowledge(db, db.get(OrderIntent, "claim-fixture"), kalshi_snapshot(
            filled="1", remaining="0", state="FILLED",
        )["order"])
    with recovery_db() as db:
        assert db.get(Position, "claim-fixture") is None
        assert pending_exposure(db, "BROKER_DEMO", "KALSHI")
        assert not pending_exposure(db, "BROKER_DEMO", "DERIV")
        assert not pending_exposure(db, "PAPER")


async def test_kalshi_fill_cannot_be_attributed_to_another_order(recovery_db):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    now = utcnow()
    decision = Decision(strategy=STRATEGY, version="1", reason="TEST", side="YES",
                        price=Decimal("0.40"), quantity=Decimal("1"), orderable=True,
                        exposure=Decimal("0.40"), instrument=MARKET)
    fields = dict(mode="BROKER_DEMO", exchange="KALSHI", strategy=STRATEGY,
                  instrument=MARKET, run_id="run-current", timestamp=now)
    with recovery_db.begin() as db:
        db.add(Signal(id="other-signal", direction="YES", reason="TEST", **fields))
        db.flush()
        intent = OrderIntent(id="other-claim", signal_id="other-signal", status="SUBMITTING",
                             request={"decision": decision.as_dict()}, **fields)
        db.add(intent)
        db.flush()
        order = demo_execution.acknowledge(db, intent, {
            **kalshi_snapshot()["order"], "broker_id": "other-broker", "client_order_id": intent.id,
        })
        demo_execution.record_fill(db, order, key="fill-one", broker_id="fill-one", quantity=Decimal("0.5"),
                                   price=Decimal("0.38"), fee=Decimal("0.01"), timestamp=now)
    snapshot = kalshi_snapshot(filled="0.5", remaining="0.5", state="PARTIAL", fee="0.01", fills=[fill])
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        await apply_kalshi(recovery_db, snapshot)
    with recovery_db() as db:
        assert db.get(Position, "claim-fixture") is None
        assert db.get(Order, "claim-fixture").status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Fill)) == 1


# CD-07: transaction-scoped worker lease; real isolated databases, mocked step.
@pytest.fixture
def lease_engine(recovery_db, monkeypatch):
    engine = recovery_db.kw["bind"]
    monkeypatch.setattr(worker, "engine", engine)
    return engine


async def test_worker_lease_success_releases_connection(lease_engine, monkeypatch):
    tick = AsyncMock(return_value=0.25)
    monkeypatch.setattr(worker, "step", tick)
    ticker = Mock()
    for _ in range(3):
        assert await worker.leased_step(ticker) == 0.25
    assert tick.await_count == 3
    tick.assert_awaited_with(ticker)
    if lease_engine.dialect.name == "postgresql":
        with lease_engine.begin() as connection:
            assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": worker.LEASE_KEY})


@pytest.mark.parametrize("failure", ["database", "unexpected", "cancelled"])
async def test_worker_lease_released_after_failure(lease_engine, monkeypatch, failure):
    errors = {
        "database": OperationalError("SELECT 1", {}, RuntimeError("fixture")),
        "unexpected": RuntimeError("fixture"),
        "cancelled": asyncio.CancelledError(),
    }
    tick = AsyncMock(side_effect=[errors[failure], 0.5])
    monkeypatch.setattr(worker, "step", tick)
    with pytest.raises(type(errors[failure])):
        await worker.leased_step()
    if lease_engine.dialect.name == "postgresql":
        with lease_engine.begin() as connection:
            assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": worker.LEASE_KEY})
    assert await worker.leased_step() == 0.5
    assert tick.await_count == 2


async def test_worker_lease_actual_contention_and_cancellation(lease_engine, monkeypatch):
    if lease_engine.dialect.name != "postgresql":
        # SQLite does not provide the PostgreSQL advisory-lock contract.
        tick = AsyncMock(return_value=0.5)
        monkeypatch.setattr(worker, "step", tick)
        assert await worker.leased_step() == 0.5
        tick.assert_awaited_once_with(None)
        return

    entered = asyncio.Event()
    hold = asyncio.Event()
    calls = []

    async def tick(_ticker):
        calls.append("entered")
        entered.set()
        await hold.wait()
        return 0.5

    monkeypatch.setattr(worker, "step", tick)
    task = asyncio.create_task(worker.leased_step())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # A distinct connection cannot run a second step during the first.
        assert await worker.leased_step() == worker.MARKET_TICK
        assert calls == ["entered"]
        with lease_engine.begin() as connection:
            assert not connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": worker.LEASE_KEY},
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    hold.set()
    assert await worker.leased_step() == 0.5
    assert calls == ["entered", "entered"]


async def test_worker_lease_database_failure_never_runs_step(lease_engine, monkeypatch):
    tick = AsyncMock()
    monkeypatch.setattr(worker, "step", tick)
    failed = Mock()
    failed.begin.side_effect = OperationalError("CONNECT", {}, RuntimeError("fixture"))
    monkeypatch.setattr(worker, "engine", failed)
    with pytest.raises(OperationalError):
        await worker.leased_step()
    tick.assert_not_awaited()


# CD-08: persisted execution identity, configuration and observation boundaries.
@pytest.mark.parametrize("kind", [
    "missing-run", "missing-risk", "missing-strategy", "wrong-run-mode",
    "wrong-run-exchange", "wrong-run-instrument", "wrong-run-strategy",
    "wrong-run-id", "invalid-desired-state", "disabled-connection",
    "missing-credentials", "disconnected-connection",
])
async def test_worker_invalid_context_fails_before_venue_or_observation(recovery_db, venue, monkeypatch, kind):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        run = db.get(StrategyRun, bot.run_id)
        connection = db.get(ExchangeConnection, "KALSHI")
        if kind == "missing-run":
            db.delete(run)
        elif kind == "missing-risk":
            db.delete(db.get(RiskConfig, 1))
        elif kind == "missing-strategy":
            db.delete(db.get(StrategyConfig, STRATEGY))
        elif kind == "wrong-run-mode":
            run.mode = "PAPER"
        elif kind == "wrong-run-exchange":
            run.exchange = "DERIV"
        elif kind == "wrong-run-instrument":
            run.instrument = "OTHER-MARKET"
        elif kind == "wrong-run-strategy":
            run.strategy = "KALSHI-STRAT-02"
        elif kind == "wrong-run-id":
            run.run_id = "other-run"
        elif kind == "invalid-desired-state":
            bot.desired_state = "INVALID"
        elif kind == "disabled-connection":
            connection.enabled = False
        elif kind == "missing-credentials":
            connection.ciphertext = None
        else:
            connection.status = "DISCONNECTED"
    factory = Mock(side_effect=AssertionError("invalid context must not construct venue"))
    monkeypatch.setattr(worker, "client", factory)
    assert await worker.step() == worker.MARKET_TICK
    factory.assert_not_called()
    worker.observe.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.last_error == "INVALID_CONFIGURATION_OR_SOURCE"
        assert bot.cursor == 0
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.parametrize("kind", [
    "run-version", "run-scope", "bot-and-run-scope", "credentials", "connection-disabled", "connection-status",
])
async def test_worker_discards_observation_when_context_changes(recovery_db, venue, monkeypatch, kind):
    venue.verify_demo = AsyncMock(return_value={
        "currency": "USD", "balance": Decimal("100"), "source": "KALSHI_DEMO", "environment": "BROKER_DEMO",
    })
    monkeypatch.setattr(worker, "has_unmanaged_exposure", AsyncMock(return_value=False))
    save_market = Mock(side_effect=AssertionError("stale observation must not persist"))
    monkeypatch.setattr(worker, "save_market", save_market)

    async def changed(*_args, **_kwargs):
        with recovery_db.begin() as db:
            bot = db.get(Bot, 1)
            run = db.get(StrategyRun, bot.run_id)
            connection = db.get(ExchangeConnection, "KALSHI")
            if kind == "run-version":
                run.strategy_version += 1
            elif kind == "run-scope":
                run.instrument = "OTHER-MARKET"
            elif kind == "bot-and-run-scope":
                bot.instrument = run.instrument = "OTHER-MARKET"
            elif kind == "credentials":
                connection.ciphertext = "changed-fixture"
            elif kind == "connection-disabled":
                connection.enabled = False
            else:
                connection.status = "DISCONNECTED"
        return Mock()

    monkeypatch.setattr(worker, "observe", AsyncMock(side_effect=changed))
    assert await worker.step() == worker.MARKET_TICK
    save_market.assert_not_called()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 0
        assert bot.state == "ERROR" and bot.desired_state == "PAUSED"
        assert bot.last_error == "INVALID_CONFIGURATION_OR_SOURCE"
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


async def test_worker_missing_live_observation_does_not_expire_replay(recovery_db, venue, monkeypatch):
    venue.verify_demo = AsyncMock(return_value={
        "currency": "USD", "balance": Decimal("100"), "source": "KALSHI_DEMO", "environment": "BROKER_DEMO",
    })
    monkeypatch.setattr(worker, "has_unmanaged_exposure", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "observe", AsyncMock(return_value=None))
    expire = Mock(side_effect=AssertionError("live run is not a finite replay"))
    monkeypatch.setattr(worker, "expire_replay", expire)
    assert await worker.step() == worker.MARKET_TICK
    expire.assert_not_called()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 0 and bot.desired_state == "RUNNING"
        assert bot.state == "DISCONNECTED" and bot.last_error == "STALE_MARKET_DATA"
        assert db.get(StrategyRun, bot.run_id).finished_at is None
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


# CD-09: preflight every current-run reconciliation candidate before remote I/O.
@pytest.mark.parametrize("kind", ["mode", "exchange", "strategy", "instrument"])
async def test_reconciliation_rejects_wrong_claim_scope(recovery_db, venue, kind):
    seed_claim(recovery_db, status="UNKNOWN")
    values = {"mode": "PAPER", "exchange": "DERIV", "strategy": "KALSHI-STRAT-02", "instrument": "OTHER"}
    with recovery_db.begin() as db:
        setattr(db.get(OrderIntent, "claim-fixture"), kind, values[kind])
    assert await worker.step() == worker.MARKET_TICK
    venue.find_demo_order.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(OrderIntent, "claim-fixture").status == "UNKNOWN"
        bot = db.get(Bot, 1)
        assert bot.desired_state == "PAUSED" and bot.state in {"RISK_BLOCKED", "ERROR"}
        assert bot.last_error == "INVALID_CONFIGURATION_OR_SOURCE"
        assert db.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.parametrize("kind", [
    "mode", "exchange", "strategy", "instrument", "source", "currency", "broker-missing",
    "broker-blank", "broker-whitespace", "intent-scope", "intent-state",
])
async def test_reconciliation_rejects_wrong_order_scope_before_remote(recovery_db, venue, kind):
    seed_kalshi_order(recovery_db)
    values = {
        "mode": "PAPER", "exchange": "DERIV", "strategy": "KALSHI-STRAT-02", "instrument": "OTHER",
        "source": "SIMULATED_FIXTURE", "currency": "EUR",
    }
    with recovery_db.begin() as db:
        order = db.get(Order, "claim-fixture")
        if kind in values:
            setattr(order, kind, values[kind])
        elif kind.startswith("broker-"):
            order.broker_id = {"broker-missing": None, "broker-blank": "", "broker-whitespace": " broker"}[kind]
        elif kind == "intent-scope":
            db.get(OrderIntent, order.intent_id).instrument = "OTHER"
        else:
            db.get(OrderIntent, order.intent_id).status = "REJECTED"
    with pytest.raises(ValueError, match="INVALID_RECONCILIATION_CONTEXT"):
        with recovery_db.begin() as db:
            await demo_execution.reconcile(db, db.get(Bot, 1), venue, cancel=True)
    venue.find_demo_order.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(Order, "claim-fixture").status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Fill)) == 0


@pytest.mark.parametrize("kind", ["wrong-adapter", "unknown-adapter", "paper", "no-run"])
async def test_reconciliation_rejects_invalid_router(recovery_db, venue, kind):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        selected = venue
        if kind == "wrong-adapter":
            selected = Mock(spec=Deriv)
        elif kind == "unknown-adapter":
            selected = Mock()
        elif kind == "paper":
            bot.mode = "PAPER"
        else:
            bot.run_id = None
        with pytest.raises(ValueError, match="INVALID_RECONCILIATION_CONTEXT"):
            await demo_execution.reconcile(db, bot, selected, cancel=True)
    venue.find_demo_order.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()


async def test_reconciliation_checks_all_orders_before_recovering_valid_claim(recovery_db, venue):
    seed_kalshi_order(recovery_db)
    with recovery_db.begin() as db:
        db.get(Order, "claim-fixture").instrument = "OTHER"
        original = db.get(OrderIntent, "claim-fixture")
        db.add(OrderIntent(
            id="pending-other", signal_id=original.signal_id, mode=original.mode,
            exchange=original.exchange, strategy=original.strategy, instrument=original.instrument,
            run_id=original.run_id, status="UNKNOWN", request=original.request, risk=original.risk,
        ))
    with pytest.raises(ValueError, match="INVALID_RECONCILIATION_CONTEXT"):
        with recovery_db.begin() as db:
            await demo_execution.reconcile(db, db.get(Bot, 1), venue, cancel=True)
    venue.find_demo_order.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(OrderIntent, "pending-other").status == "UNKNOWN"


async def test_reconciliation_ignores_terminal_cancelled_kalshi_order(recovery_db, venue):
    seed_claim(recovery_db)
    with recovery_db.begin() as db:
        demo_execution.acknowledge(db, db.get(OrderIntent, "claim-fixture"), recovered_ack())
        await demo_execution.reconcile(db, db.get(Bot, 1), venue, cancel=True)
    venue.find_demo_order.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()


async def test_reconciliation_valid_deriv_route_is_read_only(recovery_db):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        bot.exchange, bot.strategy, bot.instrument = "DERIV", "DERIV-STRAT-01", "EUR/USD"
        decision = Decision(strategy=bot.strategy, version="1", reason="TEST", side="CALL",
                            price=Decimal("1"), quantity=Decimal("1"), exposure=Decimal("1"),
                            orderable=True, instrument=bot.instrument)
        fields = dict(mode=bot.mode, exchange=bot.exchange, strategy=bot.strategy,
                      instrument=bot.instrument, run_id=bot.run_id, timestamp=utcnow())
        db.add(Signal(id="deriv-router-signal", direction="CALL", reason="TEST", **fields))
        db.flush()
        intent = OrderIntent(id="deriv-router-intent", signal_id="deriv-router-signal",
                             status="SUBMITTING", request={"decision": decision.as_dict()}, **fields)
        db.add(intent)
        db.flush()
        demo_execution.acknowledge(db, intent, {
            "source": "DERIV_DEMO", "client_order_id": intent.id, "broker_id": "123",
            "status": "OPEN", "currency": "USD", "price": Decimal("1"), "quantity": Decimal("1"),
            "observed_at": int(fields["timestamp"].timestamp()),
        })
    venue = Mock(spec=Deriv)
    venue.reconcile_demo = AsyncMock(return_value={
        "source": "DERIV_DEMO", "broker_id": "123", "status": "OPEN", "currency": "USD", "price": Decimal("1"),
    })
    venue.submit_demo = AsyncMock()
    with recovery_db.begin() as db:
        await demo_execution.reconcile(db, db.get(Bot, 1), venue, cancel=True)
    venue.reconcile_demo.assert_awaited_once_with("123")
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(Order, "deriv-router-intent").status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.scalar(select(func.count()).select_from(Settlement)) == 0


# CD-10: account admission must never persist invalid balance or unmanaged exposure.
@pytest.mark.parametrize("kind", ["balance-negative", "balance-nan", "balance-source", "inventory-invalid", "position", "resting"])
async def test_worker_account_admission_blocks_before_accounting(recovery_db, venue, monkeypatch, kind):
    payload = {
        "currency": "USD", "balance": Decimal("100"), "source": "KALSHI_DEMO", "environment": "BROKER_DEMO",
    }
    venue.positions = AsyncMock(return_value=[])
    venue.open_orders = AsyncMock(return_value=[])
    if kind == "balance-negative":
        payload["balance"] = "-1"
    elif kind == "balance-nan":
        payload["balance"] = "NaN"
    elif kind == "balance-source":
        payload["source"] = "KALSHI_PUBLIC"
    elif kind == "inventory-invalid":
        venue.positions.return_value = {}
    elif kind == "position":
        venue.positions.return_value = [{"quantity": Decimal("-1"), "exposure": Decimal("0.40")}]
    else:
        venue.open_orders.return_value = [{"broker_id": "fixture"}]
    venue.verify_demo = AsyncMock(return_value=payload)
    monkeypatch.setattr(worker, "observe", AsyncMock(return_value=Mock()))
    persist = Mock(side_effect=AssertionError("unverified account must not persist observation"))
    monkeypatch.setattr(worker, "save_market", persist)
    assert await worker.step() == worker.MARKET_TICK
    persist.assert_not_called()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 0 and bot.desired_state == "PAUSED" and bot.state == "ERROR"
        expected = "UNMANAGED_DEMO_EXPOSURE" if kind in {"position", "resting"} else "INVALID_CONFIGURATION_OR_SOURCE"
        assert bot.last_error == expected
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0


# CD-11: remaining statement paths and local ledger scope invariants.
async def test_kalshi_open_snapshot_reconciles_without_ledger_mutation(recovery_db, venue):
    seed_kalshi_order(recovery_db)
    venue.reconcile_demo.return_value = kalshi_snapshot()
    for _ in range(2):
        with recovery_db.begin() as db:
            await demo_execution.reconcile(db, db.get(Bot, 1), venue, cancel=False)
    assert venue.reconcile_demo.await_count == 2
    venue.cancel_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        order = db.get(Order, "claim-fixture")
        assert order.status == "OPEN" and order.remaining_quantity == Decimal("1")
        assert db.get(Position, order.id) is None
        assert db.scalar(select(func.count()).select_from(Fill)) == 0


@pytest.mark.parametrize("kind", [
    "timestamp-overflow", "zero-fill", "fractional-fill", "non-object-resolution",
    "wrong-event", "unknown-result", "non-object-envelope", "non-list-fills", "non-list-settlements",
])
async def test_kalshi_remaining_invalid_snapshot_paths(recovery_db, kind):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    resolution = kalshi_resolution()
    payload = kalshi_snapshot(filled="0.5", remaining="0", state="PARTIAL", fee="0.01",
                              fills=[fill], settlements=[resolution])
    if kind == "timestamp-overflow":
        fill["observed_at"] = 10**30
    elif kind == "zero-fill":
        fill["quantity"] = Decimal("0")
    elif kind == "fractional-fill":
        fill["quantity"] = Decimal("0.501")
    elif kind == "non-object-resolution":
        payload["settlements"] = [None]
    elif kind == "wrong-event":
        with recovery_db.begin() as db:
            order = db.get(Order, "claim-fixture")
            order.data = {**order.data, "event": "EVENT-A"}
        resolution["event_ticker"] = "EVENT-B"
    elif kind == "unknown-result":
        resolution["result"] = "UNKNOWN"
    elif kind == "non-object-envelope":
        payload = None
    elif kind == "non-list-fills":
        payload["fills"] = {}
    else:
        payload["settlements"] = {}
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        await apply_kalshi(recovery_db, payload)
    with recovery_db() as db:
        order = db.get(Order, "claim-fixture")
        assert order.status == "OPEN" and order.remaining_quantity == Decimal("1")
        assert db.get(Position, order.id) is None
        assert db.scalar(select(func.count()).select_from(Fill)) == 0
        assert db.scalar(select(func.count()).select_from(Fee)) == 0
        assert db.scalar(select(func.count()).select_from(Settlement)) == 0


@pytest.mark.parametrize("kind", ["mode", "exchange", "source", "currency", "side", "broker_id"])
async def test_kalshi_direct_guard_precedes_cancel_and_lookup(recovery_db, venue, kind):
    seed_kalshi_order(recovery_db)
    values = {"mode": "PAPER", "exchange": "DERIV", "source": "SIMULATED_FIXTURE",
              "currency": "EUR", "side": "CALL", "broker_id": None}
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        with recovery_db.begin() as db:
            order = db.get(Order, "claim-fixture")
            setattr(order, kind, values[kind])
            await demo_execution.reconcile_kalshi(db, order, venue, cancel=True)
    venue.reconcile_demo.assert_not_awaited()
    venue.cancel_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(Order, "claim-fixture").status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Fill)) == 0


@pytest.mark.parametrize("kind", [
    "position-missing", "position-quantity", "position-run", "position-instrument",
    "fill-run", "fill-strategy", "fee-run", "fee-currency", "fee-source",
])
async def test_kalshi_rejects_corrupted_booked_scope_or_position(recovery_db, kind):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one")
    payload = kalshi_snapshot(filled="0.5", remaining="0", state="PARTIAL", fee="0.01", fills=[fill])
    await apply_kalshi(recovery_db, payload)
    with recovery_db.begin() as db:
        position = db.get(Position, "claim-fixture")
        if kind == "position-missing":
            db.delete(position)
        elif kind == "position-quantity":
            position.quantity = Decimal("0.25")
        elif kind == "position-run":
            position.run_id = "other-run"
        elif kind == "position-instrument":
            position.instrument = "OTHER"
        elif kind == "fill-run":
            db.scalar(select(Fill)).run_id = "other-run"
        elif kind == "fill-strategy":
            db.scalar(select(Fill)).strategy = "KALSHI-STRAT-02"
        elif kind == "fee-run":
            db.scalar(select(Fee)).run_id = "other-run"
        elif kind == "fee-currency":
            db.scalar(select(Fee)).currency = "EUR"
        else:
            db.scalar(select(Fee)).source = "SIMULATED_FIXTURE"
    payload["settlements"] = [kalshi_resolution()]
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        await apply_kalshi(recovery_db, payload)
    with recovery_db() as db:
        assert db.get(Order, "claim-fixture").status == "PARTIAL"
        assert db.get(Settlement, "claim-fixture") is None
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.scalar(select(func.count()).select_from(Fee)) == 1


async def test_kalshi_orphan_position_without_fill_is_not_adopted(recovery_db):
    seed_kalshi_order(recovery_db)
    with recovery_db.begin() as db:
        order = db.get(Order, "claim-fixture")
        db.add(Position(
            id=order.id, order_id=order.id, mode=order.mode, exchange=order.exchange,
            strategy=order.strategy, instrument=order.instrument, run_id=order.run_id,
            source=order.source, currency="USD", side=order.side, status="OPEN",
            quantity=Decimal("0.5"), exposure=Decimal("0.19"), timestamp=utcnow(),
        ))
    with pytest.raises(ValueError, match="INVALID_KALSHI_RECONCILIATION"):
        await apply_kalshi(recovery_db, kalshi_snapshot())
    with recovery_db() as db:
        assert db.get(Position, "claim-fixture").quantity == Decimal("0.5")
        assert db.get(Order, "claim-fixture").status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Fill)) == 0
        assert db.get(Settlement, "claim-fixture") is None


@pytest.mark.parametrize("result,payout,expected_net", [("NO", "0", "-0.39"), ("SCALAR", "0.25", "-0.14")])
async def test_kalshi_alternative_resolution_exact_net(recovery_db, result, payout, expected_net):
    seed_kalshi_order(recovery_db)
    fill = kalshi_fill("fill-one", quantity="1")
    snapshot = kalshi_snapshot(filled="1", remaining="0", state="FILLED", fee="0.01",
                               fills=[fill], settlements=[kalshi_resolution(result=result, payout=payout)])
    await apply_kalshi(recovery_db, snapshot)
    with recovery_db() as db:
        row = db.get(Settlement, "claim-fixture")
        assert row.payout == Decimal(payout)
        assert row.net_pnl == Decimal(expected_net)
        assert row.fees == Decimal("0.01")
        assert db.get(Position, "claim-fixture").status == "SETTLED"


# CD-12: idle worker, error isolation and finite replay shutdown.
async def test_worker_without_bot_does_not_attempt_io(recovery_db, venue):
    with recovery_db.begin() as db:
        db.delete(db.get(Bot, 1))
    assert await worker.step() == worker.MARKET_TICK
    worker.observe.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()


async def test_worker_without_run_heartbeats_without_io(recovery_db, venue):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        bot.run_id = None
        bot.state = bot.desired_state = "STOPPED"
    assert await worker.step() == 1.0
    worker.observe.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.heartbeat_at is not None
        assert bot.run_id is None and bot.state == "STOPPED"
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


def test_worker_stale_error_does_not_mutate_current_run(recovery_db):
    worker.error_state("obsolete-run", "STALE_FIXTURE_ERROR", retryable=False)
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.last_error is None and bot.state == bot.desired_state == "RUNNING"
        assert bot.heartbeat_at is None
        assert db.scalar(select(func.count()).select_from(SystemEvent)) == 0


async def test_worker_replay_end_preserves_unresolved_position(recovery_db, venue, monkeypatch):
    from app.execution import new_order, record_fill

    seed_claim(recovery_db)
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        run = db.get(StrategyRun, bot.run_id)
        intent = db.get(OrderIntent, "claim-fixture")
        bot.mode = run.mode = intent.mode = "REPLAY"
        db.get(Signal, intent.signal_id).mode = "REPLAY"
        intent.status = "CONFIRMED"
        decision = Decision.from_dict(intent.request["decision"])
        order = new_order(db, intent, decision, status="FILLED", source="SIMULATED_FIXTURE")
        record_fill(db, order, key="fixture-fill", quantity=Decimal("1"), price=Decimal("0.40"),
                    fee=Decimal("0.01"), timestamp=utcnow())
        db.add(OrderIntent(
            id="pending-replay", signal_id=intent.signal_id, status="PROPOSED",
            mode=bot.mode, exchange=bot.exchange, strategy=bot.strategy,
            instrument=bot.instrument, run_id=bot.run_id, request=intent.request, risk=intent.risk,
        ))
    monkeypatch.setattr(worker, "observe", AsyncMock(return_value=None))
    assert await worker.step() == 1.0
    venue.reconcile_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        position = db.get(Position, "claim-fixture")
        assert bot.state == bot.desired_state == "STOPPED" and bot.cursor == 0
        assert db.get(StrategyRun, bot.run_id).finished_at is not None
        assert position.status == "UNRESOLVED" and position.exposure == Decimal("0.40")
        assert db.get(OrderIntent, "pending-replay").status == "CANCELLED"
        assert db.get(Settlement, "claim-fixture") is None
        assert db.scalar(select(func.count()).select_from(Fill)) == 1
        assert db.scalar(select(func.count()).select_from(Fee)) == 1
        assert db.scalar(select(func.count()).select_from(SystemEvent).where(
            SystemEvent.code == "FIXTURE_ENDED_WITH_OPEN_POSITIONS",
        )) == 1


async def test_worker_missing_demo_connection_fails_closed(recovery_db, venue):
    with recovery_db.begin() as db:
        db.delete(db.get(ExchangeConnection, "KALSHI"))
    assert await worker.step() == worker.MARKET_TICK
    worker.observe.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.state == "ERROR" and bot.desired_state == "PAUSED"
        assert bot.last_error == "INVALID_CONFIGURATION_OR_SOURCE"
        assert bot.cursor == 0


async def test_worker_key_error_sanitized_and_paused(recovery_db, venue, monkeypatch):
    monkeypatch.setattr(worker, "observe", AsyncMock(side_effect=KeyError("PRIVATE_FIXTURE_DETAIL")))
    assert await worker.step() == worker.MARKET_TICK
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.state == "ERROR" and bot.desired_state == "PAUSED"
        assert bot.last_error == "INVALID_CONFIGURATION_OR_SOURCE"
        assert bot.cursor == 0
        assert db.scalar(select(func.count()).select_from(SystemEvent).where(
            SystemEvent.code == "INVALID_CONFIGURATION_OR_SOURCE",
        )) == 1


async def test_worker_changed_cursor_discards_observation(recovery_db, venue, monkeypatch):
    venue.verify_demo = AsyncMock(return_value={
        "currency": "USD", "balance": Decimal("100"), "source": "KALSHI_DEMO", "environment": "BROKER_DEMO",
    })
    monkeypatch.setattr(worker, "has_unmanaged_exposure", AsyncMock(return_value=False))

    async def change(*_args, **_kwargs):
        with recovery_db.begin() as db:
            db.get(Bot, 1).cursor = 1
        return Mock()

    monkeypatch.setattr(worker, "observe", AsyncMock(side_effect=change))
    save = Mock(side_effect=AssertionError("stale observation cannot persist"))
    monkeypatch.setattr(worker, "save_market", save)
    assert await worker.step() == worker.MARKET_TICK
    save.assert_not_called()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 1 and bot.last_error is None
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


# CD-13: real worker orchestration with local ledger and simulated external I/O.
def worker_market(*, source="KALSHI_OFFICIAL_DEMO_MARKET", instrument=MARKET, exchange="KALSHI"):
    return Market(
        exchange, instrument, "MOCK-EVENT", worker.epoch(), source,
        book=Book(Decimal("0.39"), Decimal("0.40"), Decimal("20"), Decimal("20"),
                  fee_schedule_verified=True),
    )


def configure_worker_market(monkeypatch, venue, market):
    venue.verify_demo = AsyncMock(return_value={
        "currency": "USD", "balance": Decimal("100"), "source": "KALSHI_DEMO", "environment": "BROKER_DEMO",
    })
    venue.positions = AsyncMock(return_value=[])
    venue.open_orders = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "observe", AsyncMock(return_value=market))


@pytest.mark.parametrize("kind", ["exchange", "instrument", "source", "fixture-source"])
async def test_worker_rejects_mismatched_market_before_persistence(recovery_db, venue, monkeypatch, kind):
    if kind == "fixture-source":
        with recovery_db.begin() as db:
            bot = db.get(Bot, 1)
            run = db.get(StrategyRun, bot.run_id)
            bot.mode = run.mode = "REPLAY"
        market = worker_market()
    else:
        market = worker_market(**{
            kind: {"exchange": "DERIV", "instrument": "OTHER", "source": "SIMULATED_FIXTURE"}[kind],
        })
    configure_worker_market(monkeypatch, venue, market)
    assert await worker.step() == worker.MARKET_TICK
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 0 and bot.state == "ERROR" and bot.desired_state == "PAUSED"
        assert db.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
        assert db.scalar(select(func.count()).select_from(BalanceSnapshot)) == 0
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


@pytest.mark.parametrize("blocked", [False, True])
async def test_worker_claim_pipeline_with_real_risk_and_mocked_submission(recovery_db, venue, monkeypatch, blocked):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        run = db.get(StrategyRun, bot.run_id)
        bot.strategy = run.strategy = "KALSHI-STRAT-02"
        db.add(StrategyConfig(id=bot.strategy))
        db.get(RiskConfig, 1).thresholds = {"max_order_exposure": "0.10"} if blocked else {}
    market = worker_market()
    # Synthetic independent forecast at the mocked market boundary; no live service.
    forecast = Forecast(Decimal("0.80"), Decimal("0.05"), market.timestamp - 1,
                        market.timestamp + 300, "noaa-nbm", "fixture", "fixture-rules")
    market = replace(market, rules_id=forecast.rules_id, forecast=forecast)
    configure_worker_market(monkeypatch, venue, market)
    # Replace only the strategy output. Real risk, persistence and dispatch remain in use.
    decision = Decision(
        strategy="KALSHI-STRAT-02", version="1", reason="TEST", side="YES", orderable=True,
        price=Decimal("0.40"), quantity=Decimal("1"), exposure=Decimal("0.40"), payout=Decimal("1"),
        source=market.source, timestamp=market.timestamp, instrument=market.instrument, event=market.event,
        forecast=forecast,
    )
    evaluate = Mock(return_value=decision)
    monkeypatch.setattr(worker, "evaluate", evaluate)
    ticker = Mock()
    ticker.select = AsyncMock()
    ticker.latest.return_value = None

    async def submit(*_args, **kwargs):
        # A separate database session sees the committed claim before the mocked write.
        with recovery_db() as db:
            intent = db.get(OrderIntent, kwargs["client_order_id"])
            assert intent.status == "SUBMITTING"
            assert intent.request["risk_thresholds"] == {}
            assert db.get(Order, intent.id) is None
        return {**recovered_ack(), "client_order_id": kwargs["client_order_id"]}

    venue.submit_demo.side_effect = submit
    assert await worker.step(ticker) == worker.MARKET_TICK
    ticker.select.assert_awaited_once_with("run-current", venue, MARKET)
    ticker.latest.assert_called_once_with("run-current")
    assert evaluate.call_args.args[1].inventory == Decimal("0")
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 1 and bot.state == "RUNNING"
        assert db.get(ExchangeConnection, "KALSHI").last_sync is not None
        assert db.scalar(select(func.count()).select_from(MarketSnapshot)) == 1
        assert db.scalar(select(BalanceSnapshot)).balance == Decimal("100")
        if blocked:
            assert db.scalar(select(RiskViolation)).code == "ORDER_EXPOSURE_LIMIT"
            assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
            assert db.scalar(select(func.count()).select_from(Order)) == 0
        else:
            intent = db.scalar(select(OrderIntent))
            assert intent.status == "CONFIRMED"
            assert db.get(Order, intent.id).status == "CANCELLED"
            assert db.scalar(select(func.count()).select_from(Fill)) == 0
    if blocked:
        venue.submit_demo.assert_not_awaited()
    else:
        venue.submit_demo.assert_awaited_once()


async def test_worker_paused_before_observation_clears_ticker(recovery_db, venue):
    with recovery_db.begin() as db:
        db.get(Bot, 1).desired_state = "PAUSED"
    ticker = Mock()
    ticker.select = AsyncMock()
    assert await worker.step(ticker) == worker.MARKET_TICK
    ticker.select.assert_awaited_once_with(None, None, MARKET)
    worker.observe.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        assert db.get(Bot, 1).state == "PAUSED"


async def test_worker_fixture_pause_during_observation_never_persists(recovery_db, venue, monkeypatch):
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        run = db.get(StrategyRun, bot.run_id)
        bot.mode = run.mode = "REPLAY"

    async def pause(*_args, **_kwargs):
        with recovery_db.begin() as db:
            db.get(Bot, 1).desired_state = "PAUSED"
        return worker_market(source="SIMULATED_FIXTURE")

    monkeypatch.setattr(worker, "observe", AsyncMock(side_effect=pause))
    assert await worker.step() == 1.0
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 0 and bot.state == "PAUSED"
        assert db.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0


async def test_worker_balance_discrepancy_prevents_new_claim(recovery_db, venue, monkeypatch):
    market = worker_market()
    configure_worker_market(monkeypatch, venue, market)
    monkeypatch.setattr(worker, "evaluate", Mock(return_value=Decision(strategy=STRATEGY, version="1", reason="TEST")))
    assert await worker.step() == worker.MARKET_TICK
    venue.verify_demo.return_value["balance"] = Decimal("90")
    worker.evaluate.reset_mock()
    assert await worker.step() == worker.MARKET_TICK
    worker.evaluate.assert_not_called()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.state == "RISK_BLOCKED" and bot.desired_state == "PAUSED"
        assert db.scalar(select(RiskViolation)).code == "BALANCE_DISCREPANCY"
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert db.scalar(select(func.count()).select_from(BalanceSnapshot)) == 1


# CD-14: remaining worker paths and release of orphaned read-only subscriptions.
@pytest.mark.parametrize("kind", ["absent", "disappears", "no-run"])
async def test_worker_idle_context_stops_ticker(recovery_db, venue, monkeypatch, kind):
    if kind in {"absent", "no-run"}:
        with recovery_db.begin() as db:
            bot = db.get(Bot, 1)
            if kind == "absent":
                db.delete(bot)
            else:
                bot.run_id = None
                bot.state = bot.desired_state = "STOPPED"
    else:
        # Remove the actual row after the safety-barrier transaction has
        # committed, before the next transaction acquires the bot row lock.
        lock = worker.locked_bot
        calls = 0

        def disappearing_bot(db):
            nonlocal calls
            calls += 1
            if calls == 2:
                with recovery_db.begin() as other:
                    other.delete(other.get(Bot, 1))
            return lock(db)

        monkeypatch.setattr(worker, "locked_bot", disappearing_bot)

    ticker = Mock()
    ticker.select = AsyncMock()
    assert await worker.step(ticker) == (1.0 if kind == "no-run" else worker.MARKET_TICK)
    ticker.select.assert_awaited_once_with(None)
    worker.observe.assert_not_awaited()
    venue.reconcile_demo.assert_not_awaited()
    venue.submit_demo.assert_not_awaited()
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        if kind == "no-run":
            assert bot.run_id is None and bot.state == "STOPPED" and bot.heartbeat_at is not None
        else:
            assert bot is None
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert db.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


async def test_worker_deriv_public_stream_uses_native_symbol_without_credentials(recovery_db, venue, monkeypatch):
    strategy = "DERIV-STRAT-01"
    with recovery_db.begin() as db:
        bot = db.get(Bot, 1)
        run = db.get(StrategyRun, bot.run_id)
        bot.mode = run.mode = "PAPER"
        bot.exchange = run.exchange = "DERIV"
        bot.instrument = run.instrument = "EUR/USD"
        bot.strategy = run.strategy = strategy
        db.add(StrategyConfig(id=strategy))

    public_adapter = Mock(spec=Deriv)
    factory = Mock(return_value=public_adapter)
    monkeypatch.setattr(worker, "Deriv", factory)
    client = Mock(side_effect=AssertionError("paper mode must not use authenticated adapter"))
    monkeypatch.setattr(worker, "client", client)
    timestamp = worker.epoch()
    market = Market(
        "DERIV", "EUR/USD", "frxEURUSD", timestamp, "DERIV_OFFICIAL_PUBLIC",
        candles=(Candle(timestamp, Decimal("1.10"), Decimal("1.11"), Decimal("1.09"), Decimal("1.10")),),
    )
    monkeypatch.setattr(worker, "observe", AsyncMock(return_value=market))
    monkeypatch.setattr(worker, "evaluate", Mock(return_value=Decision(strategy=strategy, version="1", reason="TEST")))
    ticker = Mock()
    ticker.select = AsyncMock()
    ticker.latest.return_value = None
    assert await worker.step(ticker) == worker.MARKET_TICK
    factory.assert_called_once_with()
    client.assert_not_called()
    ticker.select.assert_awaited_once_with("run-current", public_adapter, "frxEURUSD")
    ticker.latest.assert_called_once_with("run-current")
    venue.submit_demo.assert_not_awaited()
    assert not public_adapter.mock_calls
    with recovery_db() as db:
        bot = db.get(Bot, 1)
        assert bot.cursor == 1 and bot.state == "RUNNING"
        snapshot = db.scalar(select(MarketSnapshot))
        assert snapshot.instrument == "EUR/USD" and snapshot.source == "DERIV_OFFICIAL_PUBLIC"
        assert db.scalar(select(func.count()).select_from(Signal)) == 1
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
