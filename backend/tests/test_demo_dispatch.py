"""CAU-MVP-001/CD-03: dispatch boundary; all broker operations mocked."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import demo_execution as execution
from app.domain import Decision
from app.integrations import VenueError
from app.models import ExchangeConnection, RiskConfig, StrategyConfig


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def context(monkeypatch):
    def build(exchange="KALSHI"):
        instrument = "EUR/USD" if exchange == "DERIV" else "MOCK-MARKET"
        strategy = f"{exchange}-STRAT-01"
        fields = dict(mode="BROKER_DEMO", exchange=exchange, strategy=strategy,
                      instrument=instrument, run_id="run-fixture")
        bot = SimpleNamespace(**fields, desired_state="RUNNING", state="RUNNING")
        decision = Decision(
            strategy=strategy, version="1", reason="TEST", instrument=instrument,
            side="CALL" if exchange == "DERIV" else "YES", orderable=True,
            price=Decimal("1") if exchange == "DERIV" else Decimal("0.40"),
            quantity=Decimal("1"), duration_seconds=300,
            exposure=Decimal("1") if exchange == "DERIV" else Decimal("0.40"),
            payout=Decimal("1.80") if exchange == "DERIV" else Decimal("1"),
        )
        risk = SimpleNamespace(kill_switch=False, thresholds={"min_payout_ratio": "1.40"})
        config = SimpleNamespace(enabled=True)
        connection = SimpleNamespace(enabled=True, ciphertext="fixture", status="CONNECTED")
        intent = SimpleNamespace(
            **fields, id="intent-fixture", timestamp=NOW, status="SUBMITTING",
            risk={"allowed": True}, request={"decision": decision.as_dict(),
                                             "risk_thresholds": dict(risk.thresholds),
                                             "expectancy": {"conservative_net_ev": "0.10"}}, response={},
        )
        records = {RiskConfig: risk, StrategyConfig: config, ExchangeConnection: connection}
        db = Mock()
        db.get.side_effect = lambda model, _key: records.get(model)
        venue = Mock(spec=execution.Deriv if exchange == "DERIV" else execution.Kalshi)
        venue.submit_demo = AsyncMock(return_value={"fixture": "ack"})
        factory, acknowledge, emit = Mock(return_value=venue), Mock(), Mock()
        monkeypatch.setattr(execution, "client", factory)
        monkeypatch.setattr(execution, "acknowledge", acknowledge)
        monkeypatch.setattr(execution, "emit", emit)
        monkeypatch.setattr(execution, "utcnow", lambda: NOW)
        return SimpleNamespace(db=db, bot=bot, intent=intent, risk=risk, config=config,
                               connection=connection, records=records, venue=venue,
                               factory=factory, acknowledge=acknowledge, emit=emit)
    return build


@pytest.mark.parametrize("target,field,value", [
    ("bot", "mode", "PAPER"),
    ("intent", "mode", "REPLAY"),
    ("bot", "desired_state", "PAUSED"),
    ("bot", "run_id", None),
    ("intent", "run_id", "other-run"),
    ("intent", "exchange", "DERIV"),
    ("intent", "strategy", "KALSHI-STRAT-02"),
    ("intent", "instrument", "OTHER-MARKET"),
    ("intent", "risk", {"allowed": 1}),
    ("intent", "risk", {}),
    ("risk", "kill_switch", True),
    ("risk", "thresholds", {"min_payout_ratio": "2"}),
    ("config", "enabled", False),
    ("connection", "enabled", False),
    ("connection", "ciphertext", None),
    ("connection", "status", "DISCONNECTED"),
    ("intent", "timestamp", NOW - timedelta(seconds=21)),
    ("intent", "timestamp", NOW + timedelta(seconds=1)),
])
async def test_ineligible_claim_never_calls_broker(context, target, field, value):
    c = context()
    setattr(getattr(c, target), field, value)
    await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == "CANCELLED"
    c.factory.assert_not_called()
    c.venue.submit_demo.assert_not_awaited()
    c.acknowledge.assert_not_called()
    assert c.emit.call_args.args[3] == "PRE_DISPATCH_CANCELLED"


@pytest.mark.parametrize("model", [RiskConfig, StrategyConfig, ExchangeConnection])
async def test_missing_configuration_blocks_dispatch(context, model):
    c = context()
    del c.records[model]
    await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == "CANCELLED"
    c.factory.assert_not_called()
    c.venue.submit_demo.assert_not_awaited()


@pytest.mark.parametrize("status", ["PROPOSED", "UNKNOWN", "CONFIRMED", "REJECTED", "CANCELLED"])
async def test_non_claim_status_untouched_and_not_sent(context, status):
    c = context()
    c.intent.status = status
    await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == status
    c.db.get.assert_not_called()
    c.factory.assert_not_called()
    c.venue.submit_demo.assert_not_awaited()
    c.emit.assert_not_called()


@pytest.mark.parametrize("exchange", ["DERIV", "KALSHI"])
@pytest.mark.parametrize("age", [0, 20])
async def test_eligible_claim_sends_once_with_intent_identity(context, exchange, age):
    c = context(exchange)
    # SQLite may return naive UTC; PostgreSQL returns aware UTC.
    c.intent.timestamp = (NOW - timedelta(seconds=age)).replace(tzinfo=None)
    await execution.dispatch(c.db, c.bot, c.intent)
    if exchange == "DERIV":
        c.venue.submit_demo.assert_awaited_once_with(
            "frxEURUSD", "CALL", Decimal("1"), duration=5,
            client_order_id=c.intent.id, min_payout_ratio=Decimal("1.80"),
        )
    else:
        c.venue.submit_demo.assert_awaited_once_with(
            c.intent.instrument, "YES", Decimal("0.40"), Decimal("1"),
            client_order_id=c.intent.id, post_only=False,
        )
    c.acknowledge.assert_called_once_with(c.db, c.intent, {"fixture": "ack"})


@pytest.mark.parametrize("exchange", ["DERIV", "KALSHI"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_adapter_failure_does_not_retry_or_create_order(context, exchange, unknown):
    c = context(exchange)
    c.venue.submit_demo.side_effect = VenueError("MOCK_FAILURE", outcome_unknown=unknown)
    await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == ("UNKNOWN" if unknown else "REJECTED")
    assert c.intent.response == {
        "submitted_at": execution.jsonable(NOW), "source": execution.BROKER_SOURCE,
        "code": "MOCK_FAILURE", "outcome_unknown": unknown, "failed_at": execution.jsonable(NOW),
    }
    assert c.bot.desired_state == ("PAUSED" if unknown else "RUNNING")
    assert c.bot.state == ("RISK_BLOCKED" if unknown else "RUNNING")
    c.venue.submit_demo.assert_awaited_once()
    c.acknowledge.assert_not_called()
    assert c.emit.call_args.args[3] == "MOCK_FAILURE"


async def test_invalid_ack_leaves_claim_for_recovery(context):
    c = context()
    c.acknowledge.side_effect = ValueError("INVALID_BROKER_CONFIRMATION")
    with pytest.raises(ValueError, match="INVALID_BROKER_CONFIRMATION"):
        await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == "SUBMITTING"
    c.venue.submit_demo.assert_awaited_once()
    c.acknowledge.assert_called_once()


@pytest.mark.parametrize("evidence", [None, {}, [], {"conservative_net_ev": None},
    {"conservative_net_ev": "0"}, {"conservative_net_ev": "-0.01"},
    {"conservative_net_ev": "NaN"}, {"conservative_net_ev": "Infinity"},
    {"conservative_net_ev": "invalid"}, {"conservative_net_ev": True},
    {"conservative_net_ev": 0.1}])
async def test_unproven_expectancy_never_dispatches(context, evidence):
    c = context()
    c.intent.request["expectancy"] = evidence
    await execution.dispatch(c.db, c.bot, c.intent)
    assert c.intent.status == "CANCELLED"
    c.factory.assert_not_called()
    c.venue.submit_demo.assert_not_awaited()
    c.acknowledge.assert_not_called()
