"""Durable worker, settlement and fail-closed tests using local fixtures."""

from asyncio import Event, wait_for
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import worker
from app.db import Base, utcnow
from app.demo_execution import dispatch
from app.integrations import Deriv, Kalshi
from app.market_data import MarketTicker, epoch
from app.models import (
    Bot, Fill, Order, OrderIntent, Position, ProbabilityEstimate, RiskConfig, Settlement,
    Signal, StrategyConfig, StrategyRun,
)

STRATEGY = "KALSHI-STRAT-01"
INSTRUMENT = "FIXTURE-TEMP-NYC"


@pytest.fixture
def db_factory(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([Bot(), RiskConfig(), StrategyConfig(id=STRATEGY)])
    monkeypatch.setattr(worker, "SessionLocal", factory)
    yield factory
    engine.dispose()


def launch(factory, mode):
    with factory.begin() as db:
        run = StrategyRun(
            mode=mode, exchange="KALSHI", strategy=STRATEGY, instrument=INSTRUMENT,
            parameters={}, strategy_version=1,
        )
        db.add(run)
        db.flush()
        run.run_id = run.id
        bot = db.get(Bot, 1)
        bot.mode, bot.exchange, bot.strategy, bot.instrument = mode, "KALSHI", STRATEGY, INSTRUMENT
        bot.state, bot.desired_state, bot.run_id = "STARTING", "RUNNING", run.id
        bot.cursor = 0
        return run.id


@pytest.mark.parametrize("mode", ["REPLAY", "PAPER"])
async def test_fixture_run_persists_money_and_calibration(db_factory, mode):
    run_id = launch(db_factory, mode)
    for _ in range(90):
        await worker.step()
        with db_factory() as db:
            if db.get(Bot, 1).state == "STOPPED":
                break
    with db_factory() as db:
        assert db.get(Bot, 1).state == "STOPPED"
        assert db.get(StrategyRun, run_id).state == "STOPPED"
        records = list(db.scalars(select(Settlement).where(Settlement.run_id == run_id)))
        assert records
        assert all(entry.mode == mode and "SIMULATED_FIXTURE" in entry.source for entry in records)
        assert all(entry.net_pnl == entry.gross_pnl - entry.fees for entry in records)
        assert db.scalar(select(func.count()).select_from(Fill)) >= len(records)
        assert db.scalar(select(func.count()).select_from(ProbabilityEstimate).where(
            ProbabilityEstimate.brier_score.is_not(None),
        )) > 0
        before = db.scalar(select(func.count()).select_from(Order))
    await worker.step()
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == before


async def test_kill_switch_cancels_existing_intent_before_fill(db_factory):
    launch(db_factory, "REPLAY")
    await worker.step()
    with db_factory.begin() as db:
        pending = db.scalar(select(OrderIntent))
        assert pending is not None and pending.status == "PROPOSED"
        db.get(RiskConfig, 1).kill_switch = True
    await worker.step()
    with db_factory() as db:
        assert db.get(Bot, 1).state == "RISK_BLOCKED"
        assert db.scalar(select(OrderIntent)).status == "CANCELLED"
        assert db.scalar(select(func.count()).select_from(Position)) == 0


async def test_dispatch_skips_invalid_execution_mode(db_factory):
    run_id = launch(db_factory, "REPLAY")
    with db_factory.begin() as db:
        bot = db.get(Bot, 1)
        fields = dict(mode=bot.mode, exchange=bot.exchange, strategy=bot.strategy,
                      instrument=bot.instrument, run_id=run_id, timestamp=utcnow())
        signal = Signal(**fields, id="s", direction="YES", reason="TEST")
        db.add(signal)
        db.flush()
        intent = OrderIntent(**fields, id="i", signal_id=signal.id, status="SUBMITTING")
        db.add(intent)
        await dispatch(db, bot, intent)
        assert intent.status == "CANCELLED"
        assert db.scalar(select(func.coalesce(func.sum(Order.exposure), Decimal("0")))) == 0




async def test_ticker_is_bound_to_run_and_discards_stale_marks():
    heard = Event()

    class DemoTicker(Deriv):
        async def stream_ticks(self, instrument):
            yield {"instrument": instrument, "price": Decimal("1.24"),
                   "observed_at": epoch() - 60, "source": "DERIV_PUBLIC"}
            heard.set()
            await Event().wait()

    feed = MarketTicker()
    await feed.select("run", DemoTicker(), "frxEURUSD")
    try:
        await wait_for(heard.wait(), timeout=1)
        assert feed.latest("other") is None
        assert feed.latest("run") is None
    finally:
        await feed.select(None)


@pytest.mark.parametrize("certified", [True, False])
async def test_sibling_discovery_requires_exchange_certified_matched_terms(certified):
    from app.integrations.kalshi import _market

    original = {
        "ticker": "MOCK-ONE", "event_ticker": "MOCK-EVENT",
        "market_type": "binary", "status": "active",
        "close_time": "2026-11-20T12:00:00Z", "notional_value_dollars": "1.0000",
        "settlement_timer_seconds": 3600, "rules_primary": "Condition one",
        "rules_secondary": "Same treatment of exceptional settlement",
    }
    compatible = {**original, "ticker": "MOCK-TWO", "rules_primary": "Condition two"}
    incompatible = {**compatible, "ticker": "MOCK-THREE", "settlement_timer_seconds": 300}

    class DemoSource(Kalshi):
        async def _read(self, path, *, params=None, auth=False):
            assert path == "/events/MOCK-EVENT"
            assert params == {"with_nested_markets": "true"} and not auth
            return {"event": {
                "event_ticker": "MOCK-EVENT", "mutually_exclusive": certified,
                "collateral_return_type": "MECNET", "settlement_sources": [{"name": "Mock"}],
                "markets": [original, compatible, incompatible],
            }}

    related = await DemoSource().exclusive_siblings(_market(original, "MOCK"))
    assert [item["instrument"] for item in related] == (["MOCK-TWO"] if certified else [])
