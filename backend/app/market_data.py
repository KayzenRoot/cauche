"""Normalize closed observations from fixtures and official market endpoints."""

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from .domain import Book, Candle, DerivQuote, Market
from .fixtures import dataset
from .integrations import Deriv, Kalshi, VenueError
from .quant.types import Relation, decimal

DERIV_SYMBOLS = {"EUR/USD": "frxEURUSD", "GBP/USD": "frxGBPUSD", "USD/JPY": "frxUSDJPY"}
ZERO = Decimal("0")


def epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def fixture_enabled(mode: str, exchange: str, instrument: str) -> bool:
    return mode == "REPLAY" or (
        mode == "PAPER" and (
            instrument.startswith("FIXTURE:") or instrument == "FIXTURE-TEMP-NYC"
        )
    )


def fixture_frame(exchange: str, instrument: str, cursor: int) -> Market | None:
    frames = dataset(exchange, instrument.removeprefix("FIXTURE:"))
    return frames[cursor] if cursor < len(frames) else None


def deriv_symbol(instrument: str) -> str:
    return DERIV_SYMBOLS.get(instrument, instrument)


def candle_amount(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError("INVALID_MARKET_CANDLE")
    try:
        return decimal(value)
    except ValueError:
        raise ValueError("INVALID_MARKET_CANDLE") from None


def closed_candles(rows: list[dict], interval: int, now: int) -> tuple[Candle, ...]:
    if not isinstance(rows, list) or type(interval) is not int or interval <= 0 or type(now) is not int or now < 0:
        raise ValueError("INVALID_MARKET_CANDLE")
    by_close = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("time")) is not int or row["time"] < 0:
            raise ValueError("INVALID_MARKET_CANDLE")
        closes_at = row["time"] + interval
        if closes_at > now:
            continue
        candle = Candle(
            closes_at, *(candle_amount(row.get(key)) for key in ("open", "high", "low", "close")),
            candle_amount(row["volume"]) if row.get("volume") is not None else None,
        )
        if not candle.valid() or (candle.volume is not None and candle.volume < ZERO):
            raise ValueError("INVALID_MARKET_CANDLE")
        previous = by_close.get(closes_at)
        if previous is not None and previous != candle:
            raise ValueError("CONFLICTING_MARKET_CANDLE")
        by_close[closes_at] = candle
    return tuple(by_close[time] for time in sorted(by_close))


async def deriv_observation(instrument: str, parameters: dict) -> Market:
    venue = Deriv()
    native = deriv_symbol(instrument)
    lower, higher = await asyncio.gather(
        venue.candles(native, 300, 110),
        venue.candles(native, 900, 90),
    )
    now = epoch()
    bars = closed_candles(lower, 300, now)
    if not bars or now - bars[-1].time > 315:
        raise ValueError("STALE_MARKET_DATA")
    # Confirmation bars cannot look ahead of the primary decision timestamp.
    confirmation = closed_candles(higher, 900, bars[-1].time)
    if not confirmation or now - confirmation[-1].time > 915:
        raise ValueError("STALE_MARKET_DATA")
    stake = Decimal(str(parameters["stake"]))
    duration = int(parameters["duration_seconds"])
    quotes = []
    if duration >= 60 and duration % 60 == 0:
        for side in ("CALL", "PUT"):
            try:
                quote = await venue.quote(native, side, stake, duration=duration // 60)
                if quote.get("currency", "USD") == "USD":
                    quotes.append(DerivQuote(
                        side, Decimal(str(quote["stake"])), Decimal(str(quote["payout"])), duration,
                    ))
            except VenueError:
                # A verified absence of a price or contract leaves the product unorderable.
                continue
    # Quote reads take time too: do not publish an observation that expired
    # while waiting for them, or accept a backward clock jump.
    observed_at = epoch()
    if not 0 <= observed_at - bars[-1].time <= 315 or not 0 <= observed_at - confirmation[-1].time <= 915:
        raise ValueError("STALE_MARKET_DATA")
    return Market(
        "DERIV", instrument, native, bars[-1].time, "DERIV_OFFICIAL_PUBLIC",
        bars, confirmation, contracts=tuple(quotes),
    )


def normalize_book(meta: dict, levels: dict) -> Book | None:
    if any(levels.get(key) is None for key in ("yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size")):
        return None
    ranges = meta.get("price_ranges") or []
    # Only a uniform price grid is supported for order eligibility.
    if len(ranges) != 1:
        return None
    rate, maker_rate = meta.get("taker_fee_rate"), meta.get("maker_fee_rate")
    return Book(
        Decimal(str(levels["yes_bid"])), Decimal(str(levels["yes_ask"])),
        Decimal(str(levels["yes_bid_size"])), Decimal(str(levels["yes_ask_size"])),
        Decimal(str(ranges[0]["step"])), Decimal(str(maker_rate or ZERO)),
        Decimal(str(rate or ZERO)), fee_schedule_verified=rate is not None and maker_rate is not None,
    )


async def discover_relation(venue: Kalshi, meta: dict) -> Relation | None:
    siblings = sorted(
        await venue.exclusive_siblings(meta),
        key=lambda row: (row.get("no_ask") or Decimal("1"), row["instrument"]),
    )
    for peer in siblings[:3]:
        levels = await venue.orderbook(peer["instrument"], market=peer)
        book = normalize_book({
            **peer, "maker_fee_rate": meta.get("maker_fee_rate"), "taker_fee_rate": meta.get("taker_fee_rate"),
        }, levels)
        if book is None or not book.valid() or not book.fee_schedule_verified:
            continue
        rules = "|".join((meta["event_ticker"], meta["instrument"], peer["instrument"],
                          meta["rules_primary"], peer["rules_primary"], str(meta["rules_secondary"])))
        return Relation(
            peer["instrument"], "MUTUALLY_EXCLUSIVE", True, book,
            int(levels["observed_at"]), sha256(rules.encode()).hexdigest(),
        )
    return None


async def kalshi_observation(instrument: str, *, demo: bool, scanner: bool = False) -> Market:
    venue = Kalshi(public=not demo)
    meta, rows = await asyncio.gather(venue.market(instrument), venue.candles(instrument, 300, 120))
    now = epoch()
    outcome = None
    if meta.get("status") in {"finalized", "settled"} or meta.get("settled_at") is not None:
        outcome = {"YES": True, "NO": False}.get(meta.get("result"))
    levels = await venue.orderbook(instrument, market=meta) if meta.get("status") == "active" else {}
    relation = None
    if scanner and levels:
        try:
            relation = await discover_relation(venue, meta)
            # Confirm primary executable depth after the peer observation.
            levels = await venue.orderbook(instrument, market=meta)
        except (VenueError, ValueError, TypeError, KeyError):
            relation = None
    source = "KALSHI_OFFICIAL_DEMO_MARKET" if demo else "KALSHI_OFFICIAL_PUBLIC"
    return Market(
        "KALSHI", instrument, meta.get("event_ticker") or instrument,
        int(levels.get("observed_at", now)), source, closed_candles(rows, 300, now),
        book=normalize_book(meta, levels), outcome=outcome, relation=relation,
        rules_id=relation.rules_id if relation else "",
        closes_at=meta.get("close_time"),
    )

async def observe(mode: str, exchange: str, instrument: str, cursor: int, parameters: dict, *, strategy: str = "") -> Market | None:
    if fixture_enabled(mode, exchange, instrument):
        return fixture_frame(exchange, instrument, cursor)
    if exchange == "DERIV":
        return await deriv_observation(instrument, parameters)
    if exchange == "KALSHI":
        return await kalshi_observation(instrument, demo=mode == "BROKER_DEMO", scanner=strategy == "KALSHI-STRAT-03")
    raise ValueError("UNSUPPORTED_EXCHANGE")



class MarketTicker:
    """Bound one read-only venue subscription to the active run."""

    def __init__(self):
        self._run_id: str | None = None
        self._task: asyncio.Task | None = None
        self._latest: dict | None = None

    async def select(self, run_id: str | None, venue=None, instrument: str = "") -> None:
        if self._run_id == run_id:
            return
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._latest = None
        self._run_id = run_id
        self._task = (
            asyncio.create_task(self._consume(venue, instrument)) if run_id and venue else None
        )

    async def _consume(self, venue, instrument: str) -> None:
        subscribe = venue.stream_ticks if isinstance(venue, Deriv) else venue.stream_ticker
        while True:
            try:
                async with aclosing(subscribe(instrument)) as feed:
                    async for row in feed:
                        if row.get("status"):
                            self._latest = None
                        elif row.get("price") is not None and row.get("instrument") == instrument:
                            self._latest = {
                                "price": str(row["price"]),
                                "observed_at": int(row["observed_at"]),
                                "source": row["source"],
                            }
            except (VenueError, ValueError, KeyError, OSError):
                self._latest = None
            await asyncio.sleep(5)

    def latest(self, run_id: str) -> dict | None:
        if self._run_id == run_id and self._latest and 0 <= epoch() - self._latest["observed_at"] <= 15:
            return self._latest
        return None
