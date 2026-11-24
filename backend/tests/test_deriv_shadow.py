"""Research outcomes are timestamped separately from orders and settlement."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.deriv_shadow import capture_candidate, resolve_due_candidate
from app.domain import Decision, DerivQuote, Market
from app.execution import entry_gate, record_decision
from app.models import Bot, Order, OrderIntent, Settlement, Signal
from app.queries import Filters, research_evidence

AT = 1_780_000_000
AT_UTC = datetime.fromtimestamp(AT, UTC)
EXPIRY = AT + 300
QUOTE = DerivQuote("CALL", Decimal("5"), Decimal("9.3"), 300, AT - 1, Decimal("55"), EXPIRY)
MARKET = Market("DERIV", "EUR/USD", "frxEURUSD", AT, "DERIV_OFFICIAL_PUBLIC", contracts=(QUOTE,))
DECISION = Decision(
    "DERIV-STRAT-01", "1.0", "SIGNAL", side="CALL", orderable=True, price=Decimal("5"), duration_seconds=300,
)


def candidate():
    return capture_candidate(MARKET, DECISION, "CALL", AT_UTC)


def test_capture_excludes_fixtures_stale_and_misaligned_quotes():
    shadow = candidate()
    assert shadow["expires_at"] == EXPIRY and shadow["expiry_basis"] == "VENUE_QUOTE"
    assert shadow["next_attempt_at"] > EXPIRY
    assert "resolution" not in shadow
    assert capture_candidate(replace(MARKET, source="SIMULATED_FIXTURE"), DECISION, "CALL", AT_UTC) is None
    stale = replace(MARKET, contracts=(replace(QUOTE, observed_at=AT - 60),))
    assert capture_candidate(stale, DECISION, "CALL", AT_UTC) is None
    misaligned = replace(MARKET, contracts=(replace(QUOTE, expires_at=AT + 500),))
    assert capture_candidate(misaligned, DECISION, "CALL", AT_UTC) is None
    missing = replace(MARKET, contracts=(replace(QUOTE, expires_at=None),))
    assert capture_candidate(missing, DECISION, "CALL", AT_UTC)["expiry_basis"] == "ASSUMED_DURATION"


@pytest.fixture
def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


def test_blocked_decision_captures_candidate_when_it_is_journaled(factory, monkeypatch):
    monkeypatch.setattr("app.execution.utcnow", lambda: AT_UTC)
    decision, evidence = entry_gate(DECISION, MARKET)
    assert not decision.orderable
    with factory.begin() as db:
        bot = Bot(mode="PAPER", exchange="DERIV", strategy=DECISION.strategy, instrument=MARKET.instrument,
                  run_id="paper")
        recorded = record_decision(db, bot, MARKET, decision, 1, parameters={}, expectancy=evidence)
        assert recorded.direction == "NO_SIGNAL"
        assert recorded.data["deriv_shadow"] == candidate()


def persist(factory):
    with factory.begin() as db:
        db.add(Signal(
            id="decision", run_id=None, mode="PAPER", exchange="DERIV", instrument="EUR/USD",
            strategy="DERIV-STRAT-01", version=1, timestamp=AT_UTC, direction="NO_SIGNAL",
            reason="CALIBRATED_EV_UNAVAILABLE", data={"deriv_shadow": candidate(), "evidence": {"point": "fixed"}},
        ))


class PublicHistory:
    def __init__(self, *, missing=False, ambiguous=False):
        self.calls = []
        self.missing = missing
        self.ambiguous = ambiguous

    async def history_ticks(self, symbol, *, limit, start_at, end_at):
        self.calls.append((symbol, limit, start_at, end_at))
        prices = [(AT + 2, "55.1"), (AT + 3, "55.2")] if end_at < EXPIRY else [
            (EXPIRY - 2, "54.9"), (EXPIRY, "55.3"),
        ]
        if self.ambiguous and end_at < EXPIRY:
            prices.append((AT + 2, "55.2"))
        return [] if self.missing else [
            {"instrument": symbol, "source": "DERIV_PUBLIC", "observed_at": timestamp, "price": Decimal(price)}
            for timestamp, price in prices
        ]


@pytest.mark.asyncio
async def test_resolution_runs_without_active_bot_and_never_creates_an_order(factory):
    persist(factory)
    venue = PublicHistory()
    assert not await resolve_due_candidate(factory, venue=venue, now=EXPIRY - 1)
    assert not venue.calls
    assert await resolve_due_candidate(factory, venue=venue, now=EXPIRY + 20)
    assert set(venue.calls) == {("frxEURUSD", 1000, AT + 1, AT + 15), ("frxEURUSD", 1000, EXPIRY - 15, EXPIRY)}
    assert not await resolve_due_candidate(factory, venue=venue, now=EXPIRY + 60)
    with factory() as db:
        sample = db.get(Signal, "decision").data
        assert sample["evidence"] == {"point": "fixed"}
        assert sample["deriv_shadow"]["resolution"]["direction"] == "WIN"
        assert sample["deriv_shadow"]["resolution"]["entry_spot"] == "55.1"
        assert sample["deriv_shadow"]["resolution"]["exit_at"] == EXPIRY
        assert db.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(Settlement)) == 0
        journal = research_evidence(db, Filters(exchange="DERIV"), limit=1)
        assert journal["deriv_shadow_coverage"] == {"pending": 0, "observed": 1, "unavailable": 0}
        assert journal["items"][0]["settlement"] is None
    assert not entry_gate(DECISION, MARKET)[0].orderable


@pytest.mark.asyncio
async def test_missing_data_retries_and_ambiguous_data_never_becomes_outcome(factory):
    persist(factory)
    venue = PublicHistory(missing=True)
    assert await resolve_due_candidate(factory, venue=venue, now=EXPIRY + 20)
    assert not await resolve_due_candidate(factory, venue=venue, now=EXPIRY + 49)
    assert await resolve_due_candidate(factory, venue=PublicHistory(ambiguous=True), now=EXPIRY + 50)
    with factory() as db:
        shadow = db.get(Signal, "decision").data["deriv_shadow"]
        assert shadow["status"] == "UNAVAILABLE" and shadow["reason"] == "TICKS_AMBIGUOUS"
        assert "resolution" not in shadow
