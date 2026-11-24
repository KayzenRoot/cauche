"""CD-31: local command admission, transitions and credential guards."""

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import commands, security
from app.db import Base
from app.models import (
    AuditEvent, Bot, ExchangeConnection, OrderIntent, RiskConfig,
    Signal, StrategyConfig, StrategyRun, User,
)
from app.schemas import Launch


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Bot(), RiskConfig(),
            User(id=1, email="owner@example.test", password_hash="fixture-only"),
            StrategyConfig(id="DERIV-STRAT-01"),
            StrategyConfig(id="KALSHI-STRAT-01"),
            ExchangeConnection(exchange="DERIV"),
            ExchangeConnection(exchange="KALSHI"),
        ])
        session.commit()
        yield session
    engine.dispose()


def reject(call, status, code):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == status
    assert exc.value.detail == code


def launch(**overrides):
    values = dict(mode="REPLAY", exchange="DERIV", strategy="DERIV-STRAT-01",
                  instrument="EUR/USD")
    return Launch(**(values | overrides))


def seed_intent(db, *, mode="BROKER_DEMO", instrument="SYNTHETIC", exchange="DERIV"):
    fields = dict(mode=mode, exchange=exchange, strategy=f"{exchange}-STRAT-01",
                  instrument=instrument, run_id="fixture-run")
    signal = Signal(**fields, id="fixture-signal", direction="YES", reason="fixture")
    db.add(signal)
    db.flush()
    db.add(OrderIntent(**fields, id="fixture-intent", signal_id=signal.id,
                       status="UNKNOWN"))
    db.flush()


@pytest.mark.parametrize("model,lookup", [
    (Bot, commands.bot_row),
    (RiskConfig, commands.risk_row),
    (ExchangeConnection, lambda db: commands.query_connection(db, "DERIV")),
])
def test_missing_required_row_fails_closed(db, model, lookup):
    key = "DERIV" if model is ExchangeConnection else 1
    db.delete(db.get(model, key))
    db.flush()
    reject(lambda: lookup(db), 503, "MIGRATE_DATABASE")


@pytest.mark.parametrize("overrides,status,code", [
    ({"exchange": "KALSHI"}, 422, "STRATEGY_EXCHANGE_MISMATCH"),
    ({"strategy": "DERIV-STRAT-02"}, 409, "STRATEGY_DISABLED"),
    ({"instrument": "UNKNOWN"}, 422, "FIXTURE_NOT_FOUND"),
    ({"mode": "BROKER_DEMO"}, 422, "CONFIRMATION_REQUIRED"),
    ({"mode": "BROKER_DEMO", "confirm": True, "instrument": "FIXTURE:EUR/USD"},
     422, "FIXTURE_NOT_ALLOWED_IN_BROKER_DEMO"),
])
def test_launch_rejects_invalid_admission(db, overrides, status, code):
    reject(lambda: commands.validate_launch(db, launch(**overrides)), status, code)
    assert db.scalar(select(StrategyRun)) is None


def test_invalid_saved_strategy_configuration(db):
    config = db.get(StrategyConfig, "DERIV-STRAT-01")
    config.parameters = {"unknown_fixture_parameter": 1}
    reject(lambda: commands.validate_launch(db, launch()), 409, "STRATEGY_CONFIGURATION_INVALID")


def test_disabled_saved_strategy(db):
    db.get(StrategyConfig, "DERIV-STRAT-01").enabled = False
    reject(lambda: commands.validate_launch(db, launch()), 409, "STRATEGY_DISABLED")


@pytest.mark.parametrize("status,code", [
    ("UNVERIFIED", "BROKER_AUTHENTICATION_REQUIRED"),
    ("CONNECTED", "CREDENTIALS_UNAVAILABLE"),
])
def test_demo_admission_requires_verified_decryptable_credentials(db, monkeypatch, status, code):
    monkeypatch.setenv("CAUCHE_MASTER_KEY", Fernet.generate_key().decode())
    row = db.get(ExchangeConnection, "DERIV")
    row.enabled, row.status, row.ciphertext = True, status, "invalid-fixture-ciphertext"
    reject(lambda: commands.validate_launch(db, launch(mode="BROKER_DEMO", confirm=True)),
           409, code)
    assert db.scalar(select(StrategyRun)) is None


def test_valid_demo_admission_only_checks_local_credentials(db, monkeypatch):
    monkeypatch.setenv("CAUCHE_MASTER_KEY", Fernet.generate_key().decode())
    row = db.get(ExchangeConnection, "DERIV")
    row.enabled, row.status = True, "CONNECTED"
    row.ciphertext = security.encrypt_credentials({"app_id": "123", "token": "fixture-only"})
    config = commands.validate_launch(db, launch(mode="BROKER_DEMO", confirm=True))
    assert config.id == "DERIV-STRAT-01"
    assert db.scalar(select(StrategyRun)) is None


@pytest.mark.parametrize("mode,instrument,blocked", [
    ("PAPER", "FIXTURE:EUR/USD", False),
    ("PAPER", "SYNTHETIC", True),
    ("BROKER_DEMO", "SYNTHETIC", True),
])
def test_pending_exposure_blocks_admission_with_mode_and_exchange_scope(db, mode, instrument, blocked):
    seed_intent(db, mode=mode, instrument=instrument)
    assert commands.pending_exposure(db, mode, "DERIV") is blocked
    assert commands.pending_exposure(db, mode, "KALSHI") is False
    if blocked:
        reject(lambda: commands.validate_launch(db, launch()), 409, "UNRESOLVED_EXPOSURE")
    else:
        assert commands.validate_launch(db, launch()).id == "DERIV-STRAT-01"


@pytest.mark.parametrize("case,code", [
    ("kill", "KILL_SWITCH_ACTIVE"),
    ("disabled", "STRATEGY_DISABLED"),
    ("missing", "STRATEGY_DISABLED"),
])
def test_resume_fails_closed(db, case, code):
    bot = db.get(Bot, 1)
    bot.state = bot.desired_state = "PAUSED"
    if case == "kill":
        db.get(RiskConfig, 1).kill_switch = True
    elif case == "disabled":
        db.get(StrategyConfig, bot.strategy).enabled = False
    else:
        db.delete(db.get(StrategyConfig, bot.strategy))
    reject(lambda: commands.change_bot(db, "resume", 1), 409, code)
    assert bot.state == bot.desired_state == "PAUSED"
    assert db.scalar(select(AuditEvent)) is None


def test_resume_persists_transition_and_audit(db):
    bot = db.get(Bot, 1)
    bot.state = bot.desired_state = "PAUSED"
    commands.change_bot(db, "resume", 1)
    assert bot.state == "STARTING" and bot.desired_state == "RUNNING"
    assert db.scalars(select(AuditEvent)).one().code == "BOT_RESUME"


def test_invalid_transition_leaves_state_unchanged(db):
    reject(lambda: commands.change_bot(db, "resume", 1), 409, "INVALID_BOT_TRANSITION")
    assert db.get(Bot, 1).state == "STOPPED"


@pytest.mark.parametrize("case", ["running", "pending"])
def test_connection_change_locked_when_in_use(db, case):
    if case == "running":
        bot = db.get(Bot, 1)
        bot.mode, bot.state = "BROKER_DEMO", "RUNNING"
    else:
        seed_intent(db)
    reject(lambda: commands.lock_connection_change(db, "DERIV"), 409, "BROKER_IN_USE")
    assert commands.lock_connection_change(db, "KALSHI").exchange == "KALSHI"


@pytest.mark.parametrize("exchange,credentials", [
    ("DERIV", {"app_id": "123"}),
    ("DERIV", {"app_id": "123", "token": "   "}),
    ("DERIV", {"app_id": "123", "token": "fixture", "url": "invalid"}),
    ("DERIV", {"app_id": "x" * 101, "token": "fixture"}),
    ("DERIV", {"app_id": "123", "token": "fíxture"}),
    ("KALSHI", {"key_id": "x" * 121, "private_key": "fixture"}),
    ("KALSHI", {"key_id": "fixture", "private_key": "x" * 10001}),
])
def test_invalid_credential_fields_rejected(exchange, credentials):
    reject(lambda: commands.checked_credentials(exchange, credentials),
           422, "INVALID_CREDENTIAL_FIELDS")


def test_credential_fields_normalized_without_io():
    assert commands.checked_credentials("DERIV", {
        "app_id": " 123 ", "token": " fixture ", "account_id": " ",
    }) == {"app_id": "123", "token": "fixture"}
    assert commands.checked_credentials("KALSHI", {
        "key_id": " fixture ", "private_key": " synthetic ",
    }) == {"key_id": "fixture", "private_key": "synthetic"}
