"""Chronological research partitions and point-in-time market context."""

from collections.abc import Sequence
from dataclasses import dataclass

from .types import D, ZERO, Market


@dataclass(frozen=True)
class Window:
    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]


def walk_forward(markets: Sequence[Market]) -> tuple[Window, ...]:
    """Reserve the final event groups and use expanding earlier training windows."""
    if not markets:
        return ()
    starts = [0]
    event_ends = {
        market.event: index for index, market in enumerate(markets)
        if market.exchange == "KALSHI"
    }
    open_through = 0
    for index, market in enumerate(markets[:-1]):
        open_through = max(open_through, event_ends.get(market.event, index))
        next_time = markets[index + 1].timestamp
        if next_time < market.timestamp:
            raise ValueError("Research observations must be chronological")
        if index >= open_through and next_time > market.timestamp:
            starts.append(index + 1)
    starts.append(len(markets))
    units = len(starts) - 1
    if units < 5:
        return ()
    development = max(4, units * 4 // 5)
    first = max(1, development * 4 // 10)
    second = max(first + 1, development * 6 // 10)
    third = max(second + 1, development * 8 // 10)
    a, b, c, d = (starts[index] for index in (first, second, third, development))
    return (
        Window((0, a), (a, b), (b, c)),
        Window((0, b), (b, c), (c, d)),
        Window((0, c), (c, d), (d, len(markets))),
    )


def context(market: Market) -> tuple[str, str]:
    hour = market.timestamp % 86400 // 3600
    session = ("00–08 UTC", "08–16 UTC", "16–24 UTC")[hour // 8]
    if market.exchange == "KALSHI":
        book = market.book
        if book is None or not book.valid():
            return "BOOK_UNAVAILABLE", session
        return ("TIGHT_BOOK" if book.spread <= book.tick * 5 else "WIDE_BOOK"), session
    bars = tuple(bar for bar in market.candles if bar.closed and bar.time <= market.timestamp)[-16:]
    if len(bars) < 16 or not all(bar.valid() for bar in bars):
        return "WARM_UP", session
    if any(right.time - left.time != market.granularity for left, right in zip(bars, bars[1:])):
        return "DATA_GAP", session
    travel = sum((abs(right.close - left.close) for left, right in zip(bars, bars[1:])), ZERO)
    efficiency = abs(bars[-1].close - bars[0].close) / travel if travel else ZERO
    range_size = (max(bar.high for bar in bars) - min(bar.low for bar in bars)) / bars[-1].close
    if range_size <= D("0.0005"):
        return "COMPRESSION", session
    if efficiency >= D("0.60"):
        return "TREND", session
    if efficiency <= D("0.30"):
        return "RANGE", session
    return "TRANSITION", session
