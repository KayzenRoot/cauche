"""CD-30: local transactional accounting and simulated execution guards."""

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import execution
from app.db import Base
from app.models import (
    BalanceSnapshot, Bot, Fee, Fill, MarketSnapshot, Order, Position,
    ProbabilityEstimate, RiskViolation, Settlement, Signal, SystemEvent,
)
from app.quant.risk import check_risk
from app.quant.types import Book, Decision, Forecast, Market

D = Decimal
NOW = 1800000000


@pytest.fixture
def ledger():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        bot = Bot(mode="REPLAY", exchange="KALSHI", strategy="KALSHI-STRAT-01",
                  instrument="FIXTURE-K", run_id="fixture-run")
        db.add(bot)
        db.flush()
        market = Market("KALSHI", bot.instrument, "FIXTURE-E", NOW, "SIMULATED_FIXTURE",
                        book=Book(D("0.40"), D("0.44"), D(10), D(10),
                                  fee_schedule_verified=True))
        decision = Decision(bot.strategy, "fixture-version", "fixture",
                            source=market.source, timestamp=NOW, side="YES",
                            orderable=True, price=D("0.44"), quantity=D(2),
                            exposure=D("0.88"), payout=D(2), estimated_costs=D("0.04"),
                            instrument=market.instrument, event=market.event)
        yield db, bot, market, decision
    engine.dispose()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def record_fixture(db, bot, market, decision):
    gated, expectancy = execution.entry_gate(decision, market)
    return execution.record_decision(
        db, bot, market, gated, 1, parameters={}, expectancy=expectancy,
    )


def pending(db, bot, market, decision):
    signal = record_fixture(db, bot, market, decision)
    assert signal is not None
    risk = check_risk(decision, market)
    assert risk.allowed
    return execution.propose(db, bot, market, signal, risk, decision)


def order_for(db, bot, market, decision):
    intent = pending(db, bot, market, decision)
    return execution.new_order(db, intent, decision, status="FILLED",
                               source="REPLAY_SIMULATED_FIXTURE")


def add_fill(db, order, market, *, fee=D("0.03")):
    execution.record_fill(db, order, key="fixture-fill", quantity=D(2),
                          price=D("0.44"), fee=fee,
                          timestamp=execution.moment(market.timestamp))


def test_snapshot_persists_tick_provenance(ledger):
    db, bot, market, _ = ledger
    tick = {"price": "0.42", "observed_at": NOW - 1, "source": market.source}
    execution.save_market(db, bot, market, tick=tick)
    snapshot = db.scalars(select(MarketSnapshot)).one()
    assert snapshot.data["mark"] == "0.42"
    assert snapshot.data["stream_timestamp"] == NOW - 1
    assert snapshot.data["stream_source"] == market.source


def test_demo_missing_balance_does_not_create_snapshot(ledger):
    db, bot, market, _ = ledger
    bot.mode = "BROKER_DEMO"
    assert execution.save_balance(db, bot, market) is None
    assert count(db, BalanceSnapshot) == 0


def test_unmatched_event_is_not_marked_as_known_pnl(ledger):
    db, bot, market, decision = ledger
    order = order_for(db, bot, market, decision)
    add_fill(db, order, market, fee=D(0))
    position = db.get(Position, order.id)
    position.unrealized_pnl = D("0.12")
    assert execution.save_balance(db, bot, replace(market, event="OTHER")) == D("999.12")
    assert position.unrealized_pnl is None
    assert db.scalars(select(BalanceSnapshot)).one().unrealized_pnl is None


def test_duplicate_signal_is_not_persisted_twice(ledger):
    db, bot, market, decision = ledger
    assert record_fixture(db, bot, market, decision) is not None
    assert record_fixture(db, bot, market, decision) is None
    assert count(db, Signal) == 1


def test_duplicate_fill_preserves_quantity_exposure_and_fee(ledger):
    db, bot, market, decision = ledger
    order = order_for(db, bot, market, decision)
    add_fill(db, order, market)
    add_fill(db, order, market)
    assert count(db, Fill) == count(db, Fee) == 1
    position = db.get(Position, order.id)
    assert position.quantity == D(2)
    assert position.exposure == D("0.88")
    assert db.scalars(select(Fee)).one().amount == D("0.03")


def test_settlement_without_position_then_duplicate_is_idempotent(ledger):
    db, bot, market, decision = ledger
    order = order_for(db, bot, market, decision)
    kwargs = dict(gross=D("1.12"), payout=D(2), timestamp=execution.moment(NOW + 300))
    execution.record_settlement(db, order, **kwargs)
    assert count(db, Settlement) == 0
    assert order.status == "FILLED"
    add_fill(db, order, market)
    execution.record_settlement(db, order, **kwargs)
    execution.record_settlement(db, order, **kwargs)
    settled = db.scalars(select(Settlement)).one()
    assert settled.net_pnl == D("1.09")
    assert order.status == db.get(Position, order.id).status == "SETTLED"


def test_probability_calibration_ignores_unrelated_event(ledger):
    db, bot, market, decision = ledger
    forecast = Forecast(D("0.75"), D("0.1"), NOW - 1, NOW + 300,
                        "simulated-ensemble", "fixture", "fixture-rules")
    record_fixture(db, bot, market, replace(decision, forecast=forecast))
    execution.settle_simulation(db, bot, replace(market, event="OTHER", outcome=True))
    estimate = db.scalars(select(ProbabilityEstimate)).one()
    assert estimate.outcome is None and estimate.brier_score is None
    execution.settle_simulation(db, bot, replace(market, outcome=True))
    assert estimate.outcome is True and estimate.brier_score == D("0.0625")


def test_simulation_does_not_fill_on_same_observation(ledger):
    db, bot, market, decision = ledger
    intent = pending(db, bot, market, decision)
    execution.execute_simulation(db, bot, market, {}, can_open=True)
    assert intent.status == "PROPOSED"
    assert count(db, Order) == 0


@pytest.mark.parametrize("can_open,event", [(False, "FIXTURE-E"), (True, "OTHER")])
def test_simulation_cancels_paused_or_unrelated_intent(ledger, can_open, event):
    db, bot, market, decision = ledger
    intent = pending(db, bot, market, decision)
    execution.execute_simulation(db, bot, replace(market, timestamp=NOW + 15, event=event),
                                 {}, can_open=can_open)
    assert intent.status == "CANCELLED"
    assert count(db, Order) == count(db, Fill) == 0
    assert db.scalar(select(SystemEvent).where(SystemEvent.code == "ORDER_CANCELLED"))


def test_simulated_fill_rechecks_risk_before_persisting(ledger):
    db, bot, market, decision = ledger
    intent = pending(db, bot, market, decision)
    execution.execute_simulation(db, bot, replace(market, timestamp=NOW + 15),
                                 {"kill_switch": True}, can_open=True)
    assert intent.status == "CANCELLED"
    assert count(db, Order) == count(db, Fill) == 0
    assert db.scalars(select(RiskViolation)).one().code == "KILL_SWITCH"


def test_simulation_does_not_exceed_reserved_cost_budget(ledger):
    db, bot, market, decision = ledger
    intent = pending(db, bot, market, replace(decision, estimated_costs=D(0)))
    execution.execute_simulation(db, bot, replace(market, timestamp=NOW + 15),
                                 {}, can_open=True)
    assert intent.status == "CANCELLED"
    assert count(db, Order) == count(db, Fill) == 0
    assert db.scalar(select(SystemEvent).where(SystemEvent.code == "SIMULATION_BUDGET_CHANGED"))


def test_simulated_fill_within_reserved_budget_control(ledger):
    db, bot, market, decision = ledger
    intent = pending(db, bot, market, decision)
    execution.execute_simulation(db, bot, replace(market, timestamp=NOW + 15),
                                 {}, can_open=True)
    assert intent.status == "CONFIRMED"
    fill = db.scalars(select(Fill)).one()
    assert fill.quantity == D(2) and fill.price == D("0.44")
    assert fill.fee == D("0.0345")
    assert fill.quantity * fill.price + fill.fee <= decision.exposure + decision.estimated_costs
