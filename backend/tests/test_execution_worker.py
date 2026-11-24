"""Durable worker, settlement and fail-closed tests using local fixtures."""

from asyncio import Event, wait_for
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import worker
from app.db import Base, utcnow
from app.demo_execution import acknowledge, dispatch
from app.domain import Decision, RiskState, dataset, evaluate
from app.execution import (
    check_context, deriv_regime, deriv_settlement_calibration, entry_gate, fit_deriv_calibration, moment,
    portfolio_pause,
)
from app.queries import Filters, research_evidence
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

        evidence = research_evidence(db, Filters(mode=mode, exchange="KALSHI"), 100)
        assert evidence["total"] == len(evidence["items"]) and evidence["total"] > len(records)
        skipped = [entry for entry in evidence["items"] if entry["direction"] == "NO_SIGNAL"]
        assert skipped and all(entry["data"]["evidence"]["source"] == "SIMULATED_FIXTURE" for entry in skipped)
        assert any(entry["data"].get("resolution") for entry in skipped)
        executed = [entry for entry in evidence["items"] if entry["settlement"]]
        assert len(executed) == len(records)
        for entry in executed:
            assert entry["data"]["evidence"]["book"]["yes_ask_size"]
            assert entry["data"]["expectancy"]["conservative_net_ev"] is not None
            assert entry["execution"]["first_fill_latency_ms"] >= 0
            assert entry["execution"]["latency_basis"] == "SIMULATED_MARKET_CLOCK"
        assert research_evidence(db, Filters(mode="BROKER_DEMO"), 2)["items"] == []
        latest = research_evidence(db, Filters(mode=mode), 1)
        assert latest["items"][0]["id"] == evidence["items"][0]["id"]
        assert research_evidence(db, Filters(mode=mode), 1, 1)["items"][0]["id"] == evidence["items"][1]["id"]


def test_live_entry_requires_positive_usable_expectancy():
    quote = dataset("KALSHI", INSTRUMENT)[0]
    simulated = evaluate(STRATEGY, quote)
    assert simulated.orderable
    live = replace(quote, source="KALSHI_OFFICIAL_PUBLIC", forecast=replace(quote.forecast, source="noaa-nbm"))
    decision = evaluate(STRATEGY, live)
    allowed, stats = entry_gate(decision, live)
    assert allowed.orderable
    assert Decimal(stats["conservative_net_ev"]) > 0

    missing, stats = entry_gate(replace(decision, forecast=None), live)
    assert not missing.orderable and missing.side is None and missing.reason == "CALIBRATED_EV_UNAVAILABLE"
    assert stats["candidate_side"] == decision.side and stats["conservative_net_ev"] is None

    negative, stats = entry_gate(replace(decision, estimated_costs=decision.payout), live)
    assert not negative.orderable and negative.side is None and negative.reason == "NET_EV_NOT_POSITIVE"
    assert Decimal(stats["conservative_net_ev"]) < 0


def test_deriv_calibration_holdout_must_be_profitable_and_disjoint():
    now = utcnow()
    net = Decimal("3")
    samples = [
        (now + timedelta(days=i), now + timedelta(days=i, minutes=5), net) for i in range(60)
    ]
    qualified = fit_deriv_calibration(samples)
    assert qualified["status"] == "VALIDATED"
    assert qualified["train_count"] == 40 and qualified["holdout_count"] == 20
    assert qualified["trained_until"] < samples[40][0]
    assert Decimal(0) < qualified["uncertainty"] < qualified["probability_of_win"]
    assert fit_deriv_calibration(samples[:59])["status"] == "INSUFFICIENT_OFFICIAL_DEMO_DATA"
    dependent = [(now + timedelta(seconds=i), now + timedelta(minutes=5), net) for i in range(60)]
    assert fit_deriv_calibration(dependent)["status"] == "INSUFFICIENT_OFFICIAL_DEMO_DATA"
    losing = samples[:40] + [(opened, closed, Decimal("-5")) for opened, closed, _ in samples[40:]]
    assert fit_deriv_calibration(losing)["status"] == "HOLDOUT_CALIBRATION_FAILED"


def test_deriv_forecast_is_bound_to_current_quote_and_official_history(db_factory):
    data = dataset("DERIV", "EUR/USD")[85]
    current_quote = replace(data.deriv_quote("CALL", Decimal("5"), 300), observed_at=data.timestamp)
    market = replace(data, source="DERIV_OFFICIAL_PUBLIC", contracts=(current_quote,))
    regime = deriv_regime(market)
    assert regime["name"] != "UNAVAILABLE"
    strategy = "DERIV-STRAT-01"
    decision = Decision(
        strategy, "v1", "PULLBACK", side="CALL", orderable=True, price=Decimal("5"),
        quantity=Decimal("1"), exposure=Decimal("5"), payout=Decimal("9.3"),
        duration_seconds=300, timestamp=data.timestamp,
    )
    with db_factory.begin() as db:
        bot = db.get(Bot, 1)
        bot.instrument, bot.strategy = "EUR/USD", strategy
        empty = deriv_settlement_calibration(db, bot, market, decision, 1, {})
        assert empty["status"] == "INSUFFICIENT_OFFICIAL_DEMO_DATA"
        assert not entry_gate(decision, market, deriv_calibration=empty)[0].orderable

        for index in range(60):
            opened = moment(data.timestamp) - timedelta(days=index + 1)
            fields = dict(mode="BROKER_DEMO", exchange="DERIV", instrument="EUR/USD",
                          strategy=strategy, run_id=None, timestamp=opened)
            signal = Signal(**fields, id=f"signal-{index}", direction="CALL", reason="TEST",
                            data={"evidence": {"market_regime": regime, "parameters": {}}})
            db.add(signal)
            db.flush()
            intent = OrderIntent(**fields, id=f"intent-{index}", signal_id=signal.id, status="CONFIRMED",
                                 request={"decision": decision.as_dict()})
            db.add(intent)
            db.flush()
            order = Order(**fields, id=f"order-{index}", intent_id=intent.id, status="SETTLED", side="CALL",
                          quantity=Decimal("1"), price=Decimal("5"), exposure=Decimal("5"), source="OFFICIAL_DEMO")
            db.add(order)
            db.flush()
            db.add(Settlement(**{**fields, "timestamp": opened + timedelta(minutes=5)}, id=f"settled-{index}",
                              order_id=order.id, gross_pnl=Decimal("4.3"), fees=Decimal("0"),
                              net_pnl=Decimal("4.3"), payout=Decimal("9.3"), source="OFFICIAL_DEMO"))
        qualified = deriv_settlement_calibration(db, bot, market, decision, 1, {})
        assert qualified["status"] == "VALIDATED"
        assert entry_gate(decision, market, deriv_calibration=qualified)[0].orderable
        changed = replace(market, contracts=(replace(current_quote, payout=Decimal("9.4")),))
        assert not entry_gate(decision, changed, deriv_calibration=qualified)[0].orderable
        assert deriv_settlement_calibration(db, bot, market, decision, 2, {})["train_count"] == 0


def test_portfolio_pause_and_same_event_reservations_include_estimated_costs(db_factory):
    run_id = launch(db_factory, "PAPER")
    market = dataset("KALSHI", INSTRUMENT)[0]
    with db_factory.begin() as db:
        bot = db.get(Bot, 1)
        for index, event in enumerate((market.event, "ANOTHER-EVENT")):
            scope = dict(
                mode="PAPER", exchange="KALSHI", strategy=STRATEGY,
                instrument=f"SIBLING-{index}", run_id=run_id, timestamp=moment(market.timestamp),
            )
            signal = Signal(**scope, id=f"signal-{index}", direction="YES", reason="TEST")
            db.add(signal)
            db.flush()
            db.add(OrderIntent(
                **scope, id=f"intent-{index}", signal_id=signal.id, status="PROPOSED",
                request={"event": event, "decision": {"exposure": "2", "estimated_costs": "0.15"}},
            ))
        state = check_context(db, bot, market, Decimal("100"))
        assert state.event_exposure == Decimal("2.15")
        assert state.global_exposure == Decimal("4.30")
        assert state.market_exposure == Decimal("0")
    assert portfolio_pause(RiskState(daily_loss=Decimal("20")), {}) == "DAILY_LOSS_LIMIT"
    assert portfolio_pause(RiskState(drawdown=Decimal("50")), {}) == "DRAWDOWN_LIMIT"
    assert portfolio_pause(RiskState(daily_loss=Decimal("19.99")), {}) is None


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


def test_deriv_ack_evidence_uses_official_purchase_timestamp_and_costs(db_factory):
    observed = epoch()
    run_id = launch(db_factory, "BROKER_DEMO")
    with db_factory.begin() as db:
        scope = dict(
            mode="BROKER_DEMO", exchange="DERIV", strategy="DERIV-STRAT-01",
            instrument="EUR/USD", run_id=run_id, timestamp=moment(observed - 2),
        )
        decision = Decision(
            "DERIV-STRAT-01", "v1", "TEST", side="CALL", orderable=True, price=Decimal("4"),
            quantity=Decimal("1"), exposure=Decimal("4"), payout=Decimal("8"), timestamp=observed - 2,
        )
        signal = Signal(**scope, id="deriv-signal", direction="CALL", reason="TEST")
        db.add(signal)
        db.flush()
        intent = OrderIntent(
            **scope, id="deriv-intent", signal_id=signal.id,
            status="SUBMITTING", request={"decision": decision.as_dict()},
        )
        db.add(intent)
        db.flush()
        acknowledge(db, intent, {
            "broker_id": "TEST-BROKER-1", "status": "OPEN", "currency": "USD",
            "price": "3.98", "payout": "8.10", "fee": "0.02", "observed_at": observed,
            "quantity": "1", "source": "DERIV_DEMO", "client_order_id": intent.id,
        })
        data = research_evidence(db, Filters(mode="BROKER_DEMO", exchange="DERIV"), 1)
        observation = data["items"][0]["execution"]
        assert observation["latency_basis"] == "BROKER_TIMESTAMP"
        assert observation["first_fill_latency_ms"] == 2000
        assert Decimal(observation["slippage_per_unit"]) == Decimal("-0.02")
        assert Decimal(observation["fees_paid"]) == Decimal("0.02")
        assert Decimal(observation["broker_quoted_payout"]) == Decimal("8.10")
        assert observation["source"] == "OFFICIAL_DEMO"


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
