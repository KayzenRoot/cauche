"""Immutable observations, decisions and exact-money primitives."""

from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
from typing import Any, Protocol

D = Decimal
ZERO = D("0")
ONE = D("1")
CENT = D("0.01")
FOUR_PLACES = D("0.0001")
SIX_PLACES = D("0.000001")


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Expected a decimal number")
    try:
        number = D(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Expected a decimal number") from error
    if not number.is_finite():
        raise ValueError("Decimal must be finite")
    return number


def wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    return value


class WireRecord:
    def as_dict(self) -> dict[str, Any]:
        return wire(asdict(self))


@dataclass(frozen=True)
class Candle(WireRecord):
    time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    closed: bool = True

    def valid(self) -> bool:
        prices = (self.open, self.high, self.low, self.close)
        return (
            all(price.is_finite() and price > ZERO for price in prices)
            and self.low <= min(self.open, self.close)
            and self.high >= max(self.open, self.close)
        )


@dataclass(frozen=True)
class DerivQuote(WireRecord):
    side: str
    stake: Decimal
    payout: Decimal
    duration_seconds: int


@dataclass(frozen=True)
class Book(WireRecord):
    yes_bid: Decimal
    yes_ask: Decimal
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    tick: Decimal = CENT
    maker_fee_rate: Decimal = D("0.0175")
    taker_fee_rate: Decimal = D("0.07")
    fee_schedule_verified: bool = False

    def valid(self) -> bool:
        return (
            all(
                getattr(self, field.name).is_finite()
                for field in fields(self)
                if field.name != "fee_schedule_verified"
            )
            and ZERO < self.yes_bid < self.yes_ask < ONE
            and ZERO < self.tick < ONE
            and self.yes_bid % self.tick == ZERO
            and self.yes_ask % self.tick == ZERO
            and min(self.yes_bid_size, self.yes_ask_size) > ZERO
            and ZERO <= self.maker_fee_rate <= ONE
            and ZERO <= self.taker_fee_rate <= ONE
        )

    def bid(self, side: str) -> Decimal:
        return self.yes_bid if side == "YES" else ONE - self.yes_ask

    def ask(self, side: str) -> Decimal:
        return self.yes_ask if side == "YES" else ONE - self.yes_bid

    def ask_size(self, side: str) -> Decimal:
        return self.yes_ask_size if side == "YES" else self.yes_bid_size

    @property
    def spread(self) -> Decimal:
        return self.yes_ask - self.yes_bid


def event_fee(
    price: Decimal,
    quantity: Decimal,
    rate: Decimal,
    *,
    balance_precision: Decimal = FOUR_PLACES,
) -> Decimal:
    """Model net fee for one buy fill with no previous order rounding accumulator."""
    if not ZERO < price < ONE or quantity <= ZERO or not ZERO <= rate <= ONE:
        raise ValueError("Invalid binary fee inputs")
    if balance_precision not in {CENT, FOUR_PLACES}:
        raise ValueError("Unsupported balance precision")
    cost = price * quantity
    trading_fee = (cost * rate * (ONE - price)).quantize(
        SIX_PLACES, rounding=ROUND_CEILING
    )
    aligned_cost = (cost + trading_fee).quantize(
        balance_precision, rounding=ROUND_CEILING
    )
    return aligned_cost - cost


@dataclass(frozen=True)
class Forecast(WireRecord):
    probability: Decimal
    uncertainty: Decimal
    issued_at: int
    expires_at: int
    source: str
    version: str
    rules_id: str

    def is_usable(self, market: "Market") -> bool:
        if not self.probability.is_finite() or not self.uncertainty.is_finite():
            return False
        approved_sources = {"noaa-nbm", "nws-forecast-calibrated", "simulated-ensemble"}
        return (
            self.source in approved_sources
            and (self.source != "simulated-ensemble" or market.simulated)
            and bool(
                self.rules_id and self.rules_id == market.rules_id and self.version
            )
            and self.issued_at <= market.timestamp < self.expires_at
            and ZERO <= self.probability <= ONE
            and ZERO <= self.uncertainty <= ONE
        )


class Estimator(Protocol):
    """Produce an independently sourced, time-anchored event forecast."""

    def estimate(self, market: "Market") -> Forecast | None: ...


@dataclass(frozen=True)
class Relation(WireRecord):
    other_instrument: str
    kind: str
    verified: bool
    other_book: Book
    observed_at: int
    rules_id: str


@dataclass(frozen=True)
class Market(WireRecord):
    exchange: str
    instrument: str
    event: str
    timestamp: int
    source: str
    candles: tuple[Candle, ...] = ()
    confirmation: tuple[Candle, ...] = ()
    contracts: tuple[DerivQuote, ...] = ()
    book: Book | None = None
    forecast: Forecast | None = None
    relation: Relation | None = None
    inventory: Decimal = ZERO
    outcome: bool | None = None
    observed_sell_volume: Decimal = ZERO
    adverse: bool = False
    rules_id: str = ""
    closes_at: int | None = None
    granularity: int = 300

    @property
    def simulated(self) -> bool:
        return self.source == "SIMULATED_FIXTURE"

    def deriv_quote(
        self, side: str, stake: Decimal, duration: int
    ) -> DerivQuote | None:
        matching = [
            quote
            for quote in self.contracts
            if quote.side == side
            and quote.duration_seconds == duration
            and quote.stake.is_finite()
            and quote.stake > ZERO
            and quote.payout.is_finite()
            and quote.payout > ZERO
            and (quote.stake == stake or self.simulated)
        ]
        if len(matching) != 1:
            return None
        quote = matching[0]
        if not self.simulated or quote.stake == stake:
            return quote
        # Fixtures define a synthetic payout ratio. Broker quotes are exact-stake only.
        payout = (quote.payout * stake / quote.stake).quantize(
            CENT, rounding=ROUND_DOWN
        )
        return DerivQuote(side, stake, payout, duration)


@dataclass(frozen=True)
class Decision(WireRecord):
    strategy: str
    version: str
    reason: str
    source: str = ""
    timestamp: int | None = None
    side: str | None = None
    orderable: bool = False
    price: Decimal = ZERO
    quantity: Decimal = ZERO
    exposure: Decimal = ZERO
    payout: Decimal = ZERO
    post_only: bool = False
    duration_seconds: int = 0
    probability: Decimal | None = None
    uncertainty: Decimal | None = None
    edge: Decimal | None = None
    estimated_costs: Decimal = ZERO
    risks: tuple[str, ...] = ()
    other_instrument: str | None = None
    estimated_fill_probability: Decimal | None = None
    forecast: Forecast | None = None
    cooldown_seconds: int = 0
    instrument: str = ""
    event: str = ""

    @property
    def status(self) -> str:
        if self.side is None:
            return "NO_SIGNAL"
        return "ORDERABLE" if self.orderable else "ADVISORY"

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "status": self.status}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Decision":
        values = {key: value for key, value in payload.items() if key != "status"}
        amounts = (
            "price",
            "quantity",
            "exposure",
            "payout",
            "probability",
            "uncertainty",
            "edge",
            "estimated_costs",
            "estimated_fill_probability",
        )
        for key in amounts:
            if values.get(key) is not None:
                values[key] = decimal(values[key])
        if values.get("forecast") is not None:
            forecast = dict(values["forecast"])
            forecast["probability"] = decimal(forecast["probability"])
            forecast["uncertainty"] = decimal(forecast["uncertainty"])
            values["forecast"] = Forecast(**forecast)
        if "risks" in values:
            values["risks"] = tuple(values["risks"])
        return cls(**values)
