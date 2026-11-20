"""Prospective directional research from Deriv public ticks."""

import asyncio
from datetime import datetime
from decimal import Decimal
from math import ceil

from sqlalchemy import select

from .db import utcnow
from .domain import Decision, Market
from .integrations import Deriv, VenueError
from .models import Signal
from .serialization import jsonable

SHADOW_SOURCE = "DERIV_PUBLIC_TICK_PROXY"
TICK_WINDOW_SECONDS = 15
PUBLICATION_DELAY_SECONDS = 20
MAX_SAMPLE_AGE_SECONDS = 86_400
MAX_ATTEMPTS = 4


def capture_candidate(market: Market, decision: Decision, side: str | None, recorded_at: datetime) -> dict | None:
    """Freeze the research assumption using only the quote known at decision time."""
    if market.exchange != "DERIV" or market.source != "DERIV_OFFICIAL_PUBLIC" or side not in {"CALL", "PUT"}:
        return None
    quote = market.deriv_quote(side, decision.price, decision.duration_seconds)
    if (
        quote is None or quote.observed_at is None or quote.spot is None or not quote.spot.is_finite()
        or quote.spot <= 0 or not 60 <= quote.duration_seconds <= 3600
        or not 0 <= market.timestamp - quote.observed_at <= 10
    ):
        return None
    processed_at = ceil(recorded_at.timestamp())
    if not 0 <= processed_at - market.timestamp <= 10:
        return None
    duration_expiry = processed_at + quote.duration_seconds
    expires_at = quote.expires_at if quote.expires_at is not None else duration_expiry
    if type(expires_at) is not int or abs(expires_at - duration_expiry) > 10:
        return None
    return {
        "status": "PENDING",
        "source": SHADOW_SOURCE,
        # Deriv's public observation carries the native symbol in the event field.
        "symbol": market.event,
        "side": side,
        "processed_at": processed_at,
        "expires_at": expires_at,
        "expiry_basis": "VENUE_QUOTE" if quote.expires_at is not None else "ASSUMED_DURATION",
        "quote": quote.as_dict(),
        "attempts": 0,
        "next_attempt_at": expires_at + PUBLICATION_DELAY_SECONDS,
    }


def _edge_tick(ticks: list[dict], symbol: str, *, last: bool) -> tuple[int, Decimal] | None:
    if not ticks:
        return None
    if any(row["instrument"] != symbol or row["source"] != "DERIV_PUBLIC" for row in ticks):
        raise VenueError("TICK_SOURCE_MISMATCH")
    at = (max if last else min)(row["observed_at"] for row in ticks)
    prices = {row["price"] for row in ticks if row["observed_at"] == at}
    if len(prices) != 1:
        raise VenueError("TICKS_AMBIGUOUS")
    return at, prices.pop()


async def observe_candidate(venue: Deriv, candidate: dict) -> dict:
    """Measure strict Rise/Fall at assumed processing and the captured expiry."""
    start = candidate["processed_at"]
    expiry = candidate["expires_at"]
    symbol = candidate["symbol"]
    async with asyncio.timeout(5):
        entry_ticks, exit_ticks = await asyncio.gather(
            venue.history_ticks(symbol, limit=1000, start_at=start + 1, end_at=start + TICK_WINDOW_SECONDS),
            venue.history_ticks(symbol, limit=1000, start_at=expiry - TICK_WINDOW_SECONDS, end_at=expiry),
        )
    if len(entry_ticks) == 1000 or len(exit_ticks) == 1000:
        raise VenueError("TICK_WINDOW_TRUNCATED", retryable=True)
    entry = _edge_tick(entry_ticks, symbol, last=False)
    exit_tick = _edge_tick(exit_ticks, symbol, last=True)
    if (
        entry is None or exit_tick is None or not start < entry[0] <= start + TICK_WINDOW_SECONDS
        or not expiry - TICK_WINDOW_SECONDS <= exit_tick[0] <= expiry or entry[0] >= exit_tick[0]
    ):
        raise VenueError("TICK_COVERAGE_INCOMPLETE", retryable=True)
    direction = "FLAT"
    if entry[1] != exit_tick[1]:
        won = (exit_tick[1] > entry[1]) == (candidate["side"] == "CALL")
        direction = "WIN" if won else "LOSS"
    return jsonable({
        "direction": direction,
        "entry_at": entry[0], "entry_spot": entry[1],
        "exit_at": exit_tick[0], "exit_spot": exit_tick[1],
        "resolved_at": int(utcnow().timestamp()),
    })


async def resolve_due_candidate(db_factory, *, venue: Deriv | None = None, now: int | None = None) -> bool:
    """Settle one bounded research observation independently from active execution."""
    now = int(utcnow().timestamp()) if now is None else now
    shadow = Signal.data["deriv_shadow"]
    with db_factory() as db:
        signal = db.scalar(select(Signal).where(
            Signal.exchange == "DERIV", Signal.mode.in_(("PAPER", "BROKER_DEMO")),
            shadow["status"].as_string() == "PENDING",
            shadow["next_attempt_at"].as_integer() <= now,
        ).order_by(Signal.timestamp, Signal.id).limit(1))
        if signal is None:
            return False
        signal_id, candidate = signal.id, signal.data["deriv_shadow"]

    attempts = candidate["attempts"] + 1
    updated = {**candidate, "attempts": attempts}
    try:
        if now - candidate["expires_at"] > MAX_SAMPLE_AGE_SECONDS:
            raise VenueError("TICK_WINDOW_EXPIRED")
        updated["resolution"] = await observe_candidate(venue if venue is not None else Deriv(), candidate)
        updated["status"] = "OBSERVED"
        updated.pop("reason", None)
    except (VenueError, TimeoutError) as error:
        updated["reason"] = error.code if isinstance(error, VenueError) else "TICK_HISTORY_TIMEOUT"
        retryable = isinstance(error, TimeoutError) or error.retryable
        if retryable and attempts < MAX_ATTEMPTS:
            updated["next_attempt_at"] = now + 30 * (2 ** (attempts - 1))
        else:
            updated["status"] = "UNAVAILABLE"

    with db_factory.begin() as db:
        signal = db.scalar(select(Signal).where(Signal.id == signal_id).with_for_update())
        if signal is not None and signal.data.get("deriv_shadow") == candidate:
            signal.data = {**signal.data, "deriv_shadow": updated}
    return True
