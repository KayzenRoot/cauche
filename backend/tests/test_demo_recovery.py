"""CD-04: real transaction boundaries, no broker network or credentials."""

import os
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import demo_execution, worker
from app.db import Base, utcnow
from app.domain import Decision
from app.integrations import Kalshi, VenueError
from app.models import (
    Bot, ExchangeConnection, Fee, Fill, Order, OrderIntent, Position, RiskConfig, Settlement, Signal,
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
        # Fixed loopback host/test-only database, never DATABASE_URL or production.
        engine = create_engine(
            f"postgresql+psycopg://cauche_test@127.0.0.1:{int(port)}/cauche_test",
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
                           request={"decision": decision.as_dict(), "risk_thresholds": {}}, **fields))


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
