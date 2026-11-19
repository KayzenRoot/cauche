"""Indicators evaluated from a chronologically ordered closed-bar prefix."""

from decimal import Decimal
from typing import Sequence

from .types import D, ONE, ZERO, Candle

HUNDRED = D("100")


def mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO) / len(values)


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    coefficient = D(2) / (period + 1)
    result = mean(values[:period])
    for value in values[period:]:
        result += coefficient * (value - result)
    return result


def wilder(values: Sequence[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        return []
    smoothed = [mean(values[:period])]
    for value in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + value) / period)
    return smoothed


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal | None:
    differences = [current - previous for previous, current in zip(values, values[1:])]
    advances = wilder([max(ZERO, difference) for difference in differences], period)
    declines = wilder([max(ZERO, -difference) for difference in differences], period)
    if not advances:
        return None
    if advances[-1] == declines[-1] == ZERO:
        return D(50)
    if declines[-1] == ZERO:
        return HUNDRED
    return HUNDRED - HUNDRED / (ONE + advances[-1] / declines[-1])


def true_ranges(candles: Sequence[Candle]) -> list[Decimal]:
    return [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:])
    ]


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    ranges = wilder(true_ranges(candles), period)
    return ranges[-1] if ranges else None


def adx(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    movements = [
        (current.high - previous.high, previous.low - current.low)
        for previous, current in zip(candles, candles[1:])
    ]
    ups = wilder(
        [up if up > max(ZERO, down) else ZERO for up, down in movements], period
    )
    downs = wilder(
        [down if down > max(ZERO, up) else ZERO for up, down in movements], period
    )
    ranges = wilder(true_ranges(candles), period)
    dx = []
    for up, down, range_value in zip(ups, downs, ranges):
        if not range_value or not (up + down):
            dx.append(ZERO)
        else:
            plus, minus = HUNDRED * up / range_value, HUNDRED * down / range_value
            dx.append(HUNDRED * abs(plus - minus) / (plus + minus))
    smoothed = wilder(dx, period)
    return smoothed[-1] if smoothed else None


def bollinger(
    values: Sequence[Decimal], period: int = 20, width: Decimal = D(2)
) -> tuple[Decimal, Decimal, Decimal] | None:
    if len(values) < period:
        return None
    window = values[-period:]
    middle = mean(window)
    sigma = mean([(item - middle) ** 2 for item in window]).sqrt()
    return middle - width * sigma, middle, middle + width * sigma


def donchian(candles: Sequence[Candle], period: int) -> tuple[Decimal, Decimal] | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    return min(item.low for item in window), max(item.high for item in window)


def closed_bars(
    candles: tuple[Candle, ...], timestamp: int, granularity: int
) -> tuple[Candle, ...] | None:
    bars = tuple(item for item in candles if item.closed and item.time <= timestamp)
    if any(not item.valid() for item in bars):
        return None
    if any(
        right.time - left.time != granularity for left, right in zip(bars, bars[1:])
    ):
        return None
    if bars and timestamp - bars[-1].time >= granularity:
        return None
    return bars
