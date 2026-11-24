"""Normalize closed observations from fixtures and official market endpoints."""

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from .domain import Book, Candle, DerivQuote, Market
from .fixtures import dataset
from .integrations import Deriv, Kalshi, VenueError
from .quant.types import BookLevel, Relation, decimal

DERIV_SYMBOLS = {"EUR/USD": "frxEURUSD", "GBP/USD": "frxGBPUSD", "USD/JPY": "frxUSDJPY"}
ZERO = Decimal("0")
BOOK_LEVELS_PER_SIDE = 20


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
                    expiry, quoted_at = quote.get("expires_at"), quote.get("observed_at")
                    spot = decimal(quote["spot"]) if quote.get("spot") is not None else ZERO
                    if (type(quoted_at) is not int or quoted_at <= 0 or spot <= ZERO
                            or (expiry is not None and type(expiry) is not int)):
                        continue
                    quotes.append(DerivQuote(
                        side, decimal(quote["stake"]), decimal(quote["payout"]), duration,
                        observed_at=quoted_at, spot=spot, expires_at=expiry,
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
        "DERIV", instrument, native, observed_at, "DERIV_OFFICIAL_PUBLIC",
        bars, confirmation, contracts=tuple(
            quote for quote in quotes if 0 <= observed_at - quote.observed_at <= 10
            and (quote.expires_at is None or observed_at < quote.expires_at)
        ),
    )


BOOK_MAX_AGE = 15


def normalize_book(meta: dict, levels: dict) -> Book | None:
    if not isinstance(meta, dict) or not isinstance(levels, dict):
        raise ValueError("INVALID_MARKET_BOOK")
    keys = ("yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size")
    if any(levels.get(key) is None for key in keys):
        return None
    ranges = meta.get("price_ranges")
    # A single uniform price grid is required for order eligibility.
    if not isinstance(ranges, list) or len(ranges) != 1:
        return None
    if not isinstance(ranges[0], dict):
        raise ValueError("INVALID_MARKET_BOOK")
    rate, maker_rate = meta.get("taker_fee_rate"), meta.get("maker_fee_rate")
    values = [*(levels[key] for key in keys), ranges[0].get("step"),
              maker_rate if maker_rate is not None else ZERO,
              rate if rate is not None else ZERO]
    if any(isinstance(value, bool) or not isinstance(value, (Decimal, str, int)) for value in values):
        raise ValueError("INVALID_MARKET_BOOK")
    try:
        book = Book(*(decimal(value) for value in values),
                    fee_schedule_verified=rate is not None and maker_rate is not None)
    except ValueError:
        raise ValueError("INVALID_MARKET_BOOK") from None
    if not book.valid():
        return None
    return book


def book_timestamp(levels: dict, meta: dict, now: int) -> int:
    if (
        not isinstance(levels, dict)
        or levels.get("instrument") != meta["instrument"]
        or levels.get("event") != meta["event_ticker"]
        or levels.get("source") != meta["source"]
        or type(levels.get("observed_at")) is not int
    ):
        raise ValueError("INVALID_MARKET_CONTEXT")
    timestamp = levels["observed_at"]
    if timestamp < 0 or not 0 <= now - timestamp <= BOOK_MAX_AGE:
        raise ValueError("STALE_MARKET_DATA")
    return timestamp


async def discover_relation(venue: Kalshi, meta: dict) -> Relation | None:
    siblings = sorted(
        await venue.exclusive_siblings(meta),
        key=lambda row: (row.get("no_ask") or Decimal("1"), row["instrument"]),
    )
    for peer in siblings[:3]:
        if (
            peer["instrument"] == meta["instrument"]
            or peer.get("event_ticker") != meta["event_ticker"]
            or peer.get("source") != meta["source"]
            or peer.get("status") != "active"
            or peer.get("close_time") != meta["close_time"]
        ):
            continue
        levels = await venue.orderbook(peer["instrument"], market=peer)
        timestamp = book_timestamp(levels, peer, epoch())
        book = normalize_book({
            **peer, "maker_fee_rate": meta.get("maker_fee_rate"), "taker_fee_rate": meta.get("taker_fee_rate"),
        }, levels)
        if book is None or not book.fee_schedule_verified:
            continue
        rules = "|".join((meta["event_ticker"], meta["instrument"], peer["instrument"],
                          meta["rules_primary"], peer["rules_primary"], str(meta["rules_secondary"])))
        return Relation(
            peer["instrument"], "MUTUALLY_EXCLUSIVE", True, book,
            timestamp, sha256(rules.encode()).hexdigest(),
        )
    return None


async def kalshi_observation(instrument: str, *, demo: bool, scanner: bool = False) -> Market:
    venue = Kalshi(public=not demo)
    meta, rows = await asyncio.gather(venue.market(instrument), venue.candles(instrument, 300, 120))
    expected_source = "KALSHI_DEMO" if demo else "KALSHI_PUBLIC"
    if (
        not isinstance(meta, dict) or meta.get("instrument") != instrument
        or meta.get("source") != expected_source
        or not isinstance(meta.get("event_ticker"), str) or not meta["event_ticker"]
        or type(meta.get("close_time")) is not int
        or meta["close_time"] < 0
    ):
        raise ValueError("INVALID_MARKET_CONTEXT")
    now = epoch()
    outcome = None
    settled = meta.get("status") in {"finalized", "settled"} or meta.get("settled_at") is not None
    if settled:
        outcome = {"YES": True, "NO": False}.get(meta.get("result"))
    book = None
    levels = {}
    relation = None
    timestamp = now
    if meta.get("status") == "active" and not settled and now < meta["close_time"]:
        levels = await venue.orderbook(instrument, market=meta)
        timestamp = book_timestamp(levels, meta, epoch())
        book = normalize_book(meta, levels)
        if scanner and book is not None:
            try:
                relation = await discover_relation(venue, meta)
            except (VenueError, ValueError, TypeError, KeyError):
                relation = None
            # Refresh even after failed peer discovery; never reuse old depth
            # if this second read fails or returns an incompatible observation.
            levels = await venue.orderbook(instrument, market=meta)
            timestamp = book_timestamp(levels, meta, epoch())
            book = normalize_book(meta, levels)
        observed_at = epoch()
        if not 0 <= observed_at - timestamp <= BOOK_MAX_AGE:
            raise ValueError("STALE_MARKET_DATA")
        if observed_at >= meta["close_time"]:
            book, relation = None, None
        if relation is not None and (
            book is None or not 0 <= timestamp - relation.observed_at <= BOOK_MAX_AGE
            or not 0 <= observed_at - relation.observed_at <= BOOK_MAX_AGE
        ):
            relation = None
    source = "KALSHI_OFFICIAL_DEMO_MARKET" if demo else "KALSHI_OFFICIAL_PUBLIC"
    return Market(
        "KALSHI", instrument, meta["event_ticker"], timestamp, source,
        closed_candles(rows, 300, timestamp), book=book, outcome=outcome, relation=relation,
        rules_id=relation.rules_id if relation else "",
        closes_at=meta["close_time"],
        book_levels=tuple(
            BookLevel(side, price, size)
            for side, key in (("YES", "yes_bids"), ("NO", "no_bids"))
            for price, size in levels.get(key, [])[:BOOK_LEVELS_PER_SIDE]
        ) if book is not None else (),
    )


async def observe(mode: str, exchange: str, instrument: str, cursor: int, parameters: dict, *, strategy: str = "") -> Market | None:
    if fixture_enabled(mode, exchange, instrument):
        return fixture_frame(exchange, instrument, cursor)
    if exchange == "DERIV":
        return await deriv_observation(instrument, parameters)
    if exchange == "KALSHI":
        return await kalshi_observation(instrument, demo=mode == "BROKER_DEMO", scanner=strategy == "KALSHI-STRAT-03")
    raise ValueError("UNSUPPORTED_EXCHANGE")



TICKER_RETRY_SECONDS = 5


class MarketTicker:
    """Bind a read-only subscription to the selected run, instrument and source."""

    def __init__(self):
        self._selection: tuple[str, str, str] | None = None
        self._task: asyncio.Task | None = None
        self._latest: dict | None = None

    async def select(self, run_id: str | None, venue=None, instrument: str = "") -> None:
        source = None
        invalid = False
        if run_id and venue is not None:
            if isinstance(venue, Deriv):
                source = "DERIV_PUBLIC"
            elif isinstance(venue, Kalshi) and venue.source == "KALSHI_DEMO":
                source = "KALSHI_DEMO"
            else:
                invalid = True
        selection = (run_id, instrument, source) if run_id and instrument and source else None
        invalid = invalid or bool(run_id and venue is not None and not instrument)
        if selection == self._selection and not invalid and (
            selection is None or (self._task is not None and not self._task.done())
        ):
            return
        self._selection = None
        self._latest = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if invalid:
            raise ValueError("INVALID_TICKER_CONTEXT")
        if selection is not None:
            self._selection = selection
            self._task = asyncio.create_task(self._consume(venue, instrument, source))

    async def _consume(self, venue, instrument: str, source: str) -> None:
        subscribe = venue.stream_ticks if source == "DERIV_PUBLIC" else venue.stream_ticker
        while True:
            try:
                async with aclosing(subscribe(instrument)) as feed:
                    async for row in feed:
                        if not isinstance(row, dict):
                            raise ValueError("INVALID_MARKET_TICK")
                        if row.get("status"):
                            self._latest = None
                            continue
                        if (
                            row.get("instrument") != instrument or row.get("source") != source
                            or type(row.get("observed_at")) is not int
                            or row["observed_at"] < 0
                        ):
                            raise ValueError("INVALID_MARKET_TICK")
                        price = candle_amount(row.get("price"))
                        if price < ZERO or (source == "DERIV_PUBLIC" and price == ZERO) or (
                            source == "KALSHI_DEMO" and price > 1
                        ):
                            raise ValueError("INVALID_MARKET_TICK")
                        timestamp = row["observed_at"]
                        if not 0 <= epoch() - timestamp <= BOOK_MAX_AGE or (
                            self._latest is not None and timestamp < self._latest["observed_at"]
                        ):
                            self._latest = None
                            continue
                        self._latest = {"price": str(price), "observed_at": timestamp, "source": source}
            except (VenueError, ValueError, KeyError, TypeError, OSError):
                # Never propagate potentially private upstream details to a snapshot.
                pass
            finally:
                # EOF, transport failure and cancellation invalidate cached marks.
                self._latest = None
            await asyncio.sleep(TICKER_RETRY_SECONDS)

    def latest(self, run_id: str) -> dict | None:
        if (
            self._selection is not None and self._selection[0] == run_id and self._latest
            and 0 <= epoch() - self._latest["observed_at"] <= BOOK_MAX_AGE
        ):
            return dict(self._latest)
        return None
