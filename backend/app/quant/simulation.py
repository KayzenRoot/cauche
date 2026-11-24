"""Product-specific simulated fills and settlement."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from .types import CENT, D, ONE, ZERO, Decision, Market, WireRecord, decimal, event_fee


@dataclass(frozen=True)
class SimFill(WireRecord):
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    exposure: Decimal
    payout: Decimal
    entry_price: Decimal
    instrument: str
    exchange: str
    opened_at: int
    expires_at: int | None
    event: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SimFill":
        amounts = {"price", "quantity", "fee", "exposure", "payout", "entry_price"}
        return cls(
            **{
                key: decimal(value) if key in amounts else value
                for key, value in payload.items()
            }
        )


def simulate_fill(
    decision: Decision,
    market: Market,
    *,
    slippage: Decimal = ZERO,
    fee_rate: Decimal = ZERO,
) -> SimFill | None:
    if (
        not decision.orderable
        or decision.timestamp is None
        or market.timestamp < decision.timestamp
    ):
        return None
    if (decision.instrument, decision.event, decision.source) != (
        market.instrument,
        market.event,
        market.source,
    ):
        return None
    if not ZERO <= slippage <= D("0.5") or not ZERO <= fee_rate <= D("0.5"):
        raise ValueError("Simulation costs outside supported range")
    if market.outcome is not None or (
        market.closes_at is not None and market.timestamp >= market.closes_at
    ):
        return None
    if market.exchange == "DERIV":
        quote = market.deriv_quote(
            decision.side, decision.exposure, decision.duration_seconds
        )
        bars = [
            bar for bar in market.candles if bar.closed and bar.time <= market.timestamp
        ]
        if (
            quote is None
            or quote.payout < decision.payout
            or not bars
            or not bars[-1].valid()
        ):
            return None
        entry = bars[-1]
        if not decision.timestamp < entry.time <= market.timestamp <= entry.time + 15:
            return None
        if decision.duration_seconds % market.granularity:
            return None
        expiry = entry.time + decision.duration_seconds
        if market.closes_at is not None and expiry > market.closes_at:
            return None
        fee = decision.exposure * (fee_rate + slippage)
        return SimFill(
            decision.side,
            decision.price,
            ONE,
            fee,
            decision.exposure,
            decision.payout,
            entry.close,
            market.instrument,
            market.exchange,
            entry.time,
            expiry,
            market.event,
        )

    if market.exchange != "KALSHI" or market.book is None or not market.book.valid():
        return None
    if not market.book.fee_schedule_verified or decision.side not in {"YES", "NO"}:
        return None
    book = market.book
    if decision.post_only:
        if (
            market.timestamp == decision.timestamp
            or book.ask(decision.side) > decision.price
        ):
            return None
        quantity = min(decision.quantity, market.observed_sell_volume)
        price = decision.price
        rate = book.maker_fee_rate + fee_rate
    else:
        price = book.ask(decision.side) + slippage
        if price > decision.price or price >= ONE:
            return None
        quantity = min(decision.quantity, book.ask_size(decision.side))
        rate = book.taker_fee_rate + fee_rate
    quantity = quantity.quantize(CENT, rounding=ROUND_DOWN)
    if quantity <= ZERO:
        return None
    fee = event_fee(price, quantity, rate)
    return SimFill(
        decision.side,
        price,
        quantity,
        fee,
        price * quantity,
        quantity,
        price,
        market.instrument,
        market.exchange,
        market.timestamp,
        market.closes_at,
        market.event,
    )


def settle(fill: SimFill, closing_market: Market) -> Decimal | None:
    if (
        fill.exchange != closing_market.exchange
        or fill.instrument != closing_market.instrument
    ):
        return None
    if fill.event and fill.event != closing_market.event:
        return None
    if closing_market.timestamp < fill.opened_at:
        return None
    if fill.exchange == "KALSHI":
        if closing_market.outcome is None:
            return None
        if fill.expires_at is not None and closing_market.timestamp < fill.expires_at:
            return None
        wins = closing_market.outcome == (fill.side == "YES")
    else:
        if fill.expires_at is None or closing_market.timestamp < fill.expires_at:
            return None
        closing = [
            bar
            for bar in closing_market.candles
            if bar.time == fill.expires_at and bar.closed and bar.valid()
        ]
        if len(closing) != 1:
            return None
        if fill.side == "CALL":
            wins = closing[0].close > fill.entry_price
        else:
            wins = closing[0].close < fill.entry_price
    gross_payout = fill.payout if wins else ZERO
    return gross_payout - fill.exposure - fill.fee
