"""Persistent records for the owner, execution engine and audit feed."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from .db import Base, utcnow

MONEY = Numeric(24, 8)
DATE = DateTime(timezone=True)
STRATEGIES = {
    "DERIV-STRAT-01": "Trend Following + Pullback",
    "DERIV-STRAT-02": "Breakout + ATR",
    "DERIV-STRAT-03": "Bollinger + RSI",
    "KALSHI-STRAT-01": "Probability Edge",
    "KALSHI-STRAT-02": "Market Making",
    "KALSHI-STRAT-03": "Logical Arbitrage",
}


def uuid() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("id = 1", name="only_owner"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    created_at: Mapped[datetime] = mapped_column(DATE, default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DATE, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DATE, index=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(default=0)
    window_start: Mapped[datetime] = mapped_column(DATE, default=utcnow)


class ExchangeConnection(Base):
    __tablename__ = "exchange_connections"
    __table_args__ = (CheckConstraint("exchange IN ('DERIV','KALSHI')", name="connection_exchange"),)

    exchange: Mapped[str] = mapped_column(String(10), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="NOT_CONFIGURED")
    enabled: Mapped[bool] = mapped_column(default=False)
    ciphertext: Mapped[str | None] = mapped_column(Text)
    last_sync: Mapped[datetime | None] = mapped_column(DATE)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)


class RealAccountConnection(Base):
    """Owner-configured balance lookup, isolated from all executable connections."""

    __tablename__ = "real_account_connections"
    __table_args__ = (
        CheckConstraint("exchange IN ('DERIV','KALSHI')", name="real_account_exchange"),
    )

    exchange: Mapped[str] = mapped_column(String(10), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="NOT_CONFIGURED")
    enabled: Mapped[bool] = mapped_column(default=False)
    ciphertext: Mapped[str | None] = mapped_column(Text)
    last_sync: Mapped[datetime | None] = mapped_column(DATE)
    currency: Mapped[str | None] = mapped_column(String(20))
    balance: Mapped[Decimal | None] = mapped_column(Numeric(32, 16))


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskConfig(Base):
    __tablename__ = "risk_config"
    __table_args__ = (CheckConstraint("id = 1", name="only_risk_config"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    kill_switch: Mapped[bool] = mapped_column(default=False)
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict)


class Bot(Base):
    __tablename__ = "bot"
    __table_args__ = (
        CheckConstraint("id = 1", name="only_bot"),
        CheckConstraint("mode IN ('REPLAY','PAPER','BROKER_DEMO')", name="bot_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    state: Mapped[str] = mapped_column(String(24), default="STOPPED")
    desired_state: Mapped[str] = mapped_column(String(24), default="STOPPED")
    mode: Mapped[str] = mapped_column(String(12), default="REPLAY")
    exchange: Mapped[str] = mapped_column(String(10), default="DERIV")
    strategy: Mapped[str] = mapped_column(String(24), default="DERIV-STRAT-01")
    instrument: Mapped[str] = mapped_column(String(100), default="EUR/USD")
    run_id: Mapped[str | None] = mapped_column(String(40))
    cursor: Mapped[int] = mapped_column(default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DATE)
    updated_at: Mapped[datetime] = mapped_column(DATE, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(64))


class Scope:
    mode: Mapped[str] = mapped_column(String(12))
    exchange: Mapped[str] = mapped_column(String(10))
    strategy: Mapped[str | None] = mapped_column(String(24))
    instrument: Mapped[str] = mapped_column(String(100))
    timestamp: Mapped[datetime] = mapped_column(DATE, default=utcnow)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)

    @classmethod
    def scope_constraints(cls) -> tuple:
        return (
            CheckConstraint("mode IN ('REPLAY','PAPER','BROKER_DEMO')", name=f"{cls.__tablename__}_mode"),
            Index(f"ix_{cls.__tablename__}_scope", "mode", "exchange", "timestamp"),
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return cls.scope_constraints()


class MoneyRecord:
    currency: Mapped[str] = mapped_column(String(8), default="USD")


class StrategyRun(Scope, Base):
    __tablename__ = "strategy_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uuid)
    state: Mapped[str] = mapped_column(String(24), default="STARTING")
    finished_at: Mapped[datetime | None] = mapped_column(DATE)
    strategy_version: Mapped[int] = mapped_column(default=1)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)


class MarketSnapshot(Scope, Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MarketCandle(Scope, Base):
    __tablename__ = "market_candles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    time: Mapped[int]
    timeframe: Mapped[int] = mapped_column(default=300)
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal | None] = mapped_column(MONEY)

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            *cls.scope_constraints(),
            UniqueConstraint("mode", "exchange", "instrument", "source", "timeframe", "time",
                             name="unique_candle"),
        )


class ProbabilityEstimate(Scope, Base):
    __tablename__ = "probability_estimates"

    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(100))
    probability: Mapped[Decimal] = mapped_column(MONEY)
    uncertainty: Mapped[Decimal] = mapped_column(MONEY)
    outcome: Mapped[bool | None]
    brier_score: Mapped[Decimal | None] = mapped_column(MONEY)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Signal(Scope, Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uuid)
    version: Mapped[int] = mapped_column(default=1)
    direction: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class OrderIntent(Scope, Base):
    __tablename__ = "order_intents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    risk: Mapped[dict] = mapped_column(JSON, default=dict)
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict] = mapped_column(JSON, default=dict)


class Order(Scope, MoneyRecord, Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.id"), unique=True)
    broker_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    remaining_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            *cls.scope_constraints(),
            UniqueConstraint("mode", "exchange", "broker_id", name="unique_broker_order"),
        )


class OrderRecord:
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))


class Fill(Scope, MoneyRecord, OrderRecord, Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid)
    broker_id: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            *cls.scope_constraints(),
            UniqueConstraint("mode", "exchange", "broker_id", name="unique_broker_fill"),
        )


class Position(Scope, MoneyRecord, OrderRecord, Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid)
    side: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)


class Settlement(Scope, MoneyRecord, OrderRecord, Base):
    __tablename__ = "settlements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid)
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY)
    payout: Mapped[Decimal] = mapped_column(MONEY)


class Fee(Scope, MoneyRecord, OrderRecord, Base):
    __tablename__ = "fees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid)
    amount: Mapped[Decimal] = mapped_column(MONEY)


class BalanceSnapshot(Scope, MoneyRecord, Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    initial_balance: Mapped[Decimal] = mapped_column(MONEY)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY)
    drawdown: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))


class RiskViolation(Scope, Base):
    __tablename__ = "risk_violations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(180))
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class EventFields:
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DATE, default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(12), default="INFO")
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(10))
    mode: Mapped[str | None] = mapped_column(String(12))
    strategy: Mapped[str | None] = mapped_column(String(24))
    instrument: Mapped[str | None] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemEvent(EventFields, Base):
    __tablename__ = "system_events"
    __table_args__ = {"sqlite_autoincrement": True}


class AuditEvent(EventFields, Base):
    __tablename__ = "audit_events"
    __table_args__ = {"sqlite_autoincrement": True}
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uuid)
    exchange: Mapped[str] = mapped_column(String(10))
    strategy: Mapped[str] = mapped_column(String(24))
    instrument: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(40), default="SIMULATED_FIXTURE")
    status: Mapped[str] = mapped_column(String(16), default="COMPLETED")
    inputs: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DATE, default=utcnow, index=True)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id"), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
