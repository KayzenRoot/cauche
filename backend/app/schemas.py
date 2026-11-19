"""Validated, bounded inputs for the owner-facing API."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Exchange = Literal["DERIV", "KALSHI"]
Mode = Literal["REPLAY", "PAPER", "BROKER_DEMO"]
Instrument = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_/:.-]+$")]
StrategyId = Annotated[str, StringConstraints(pattern=r"^(DERIV|KALSHI)-STRAT-0[1-3]$")]
Email = Annotated[str, StringConstraints(max_length=254)]
Password = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Parameters = dict[Annotated[str, StringConstraints(max_length=64)], str | int | float | bool]


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value):
        raise ValueError("INVALID_EMAIL")
    return value


def boundary(value: str | None, *, upper: bool = False) -> datetime | None:
    if value is None:
        return None
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime.combine(day + timedelta(days=upper), time.min, tzinfo=UTC)
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("UTC_OFFSET_REQUIRED")
    return instant.astimezone(UTC)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class Login(Input):
    email: Email
    password: Password

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_email(value)


class Confirm(Input):
    confirm: Literal[True]


class ChangePassword(Confirm):
    current_password: Password
    new_password: Annotated[str, StringConstraints(min_length=14, max_length=128)]


class Launch(Input):
    confirm: bool = False
    mode: Mode
    exchange: Exchange
    strategy: StrategyId
    instrument: Instrument


class StrategyUpdate(Confirm):
    enabled: bool
    parameters: Parameters = Field(default_factory=dict, max_length=30)


class RiskUpdate(Confirm):
    kill_switch: bool
    thresholds: Parameters = Field(default_factory=dict, max_length=30)


class ConnectionUpdate(Confirm):
    credentials: dict[
        Annotated[str, StringConstraints(max_length=20)],
        Annotated[str, StringConstraints(min_length=1, max_length=10000)],
    ] = Field(max_length=3)
    enabled: bool = True


class BacktestInput(Input):
    exchange: Exchange
    strategy: StrategyId
    instrument: Instrument
    start: str | None = Field(default=None, max_length=40)
    end: str | None = Field(default=None, max_length=40)
    initial_balance: Decimal = Field(default=Decimal("1000"), ge=1, le=1_000_000)
    parameters: Parameters = Field(default_factory=dict, max_length=30)
    costs: Parameters = Field(default_factory=dict, max_length=12)
    risk: Parameters = Field(default_factory=dict, max_length=30)
