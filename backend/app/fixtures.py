"""Time-ordered synthetic datasets for offline execution and backtest validation."""

from collections import Counter
from functools import lru_cache

from .quant.types import D, ONE, Book, Candle, DerivQuote, Forecast, Market, Relation

SOURCE = "SIMULATED_FIXTURE"
START = 1767225600  # 2026-01-01T00:00:00Z
INSTRUMENTS = {
    "DERIV": ("EUR/USD", "GBP/USD", "USD/JPY"),
    "KALSHI": ("FIXTURE-TEMP-NYC",),
}
DEFAULT_INSTRUMENT = {exchange: names[0] for exchange, names in INSTRUMENTS.items()}


def ensemble_exceedance(
    members: tuple,
    threshold,
    *,
    issued_at: int,
    expires_at: int,
    rules_id: str,
    source: str,
) -> Forecast:
    """Estimate strict exceedance using a time-aligned ensemble and smoothed counts."""
    if len(members) < 15 or not all(value.is_finite() for value in members):
        raise ValueError("An ensemble of at least 15 finite values is required")
    if not threshold.is_finite() or expires_at <= issued_at or not rules_id:
        raise ValueError("Ensemble metadata is invalid")
    counts = Counter(value > threshold for value in members)
    probability = D(counts[True] + 1) / (len(members) + 2)
    uncertainty = (probability * (ONE - probability) / (len(members) + 2)).sqrt()
    return Forecast(
        probability,
        uncertainty,
        issued_at,
        expires_at,
        source,
        "ENSEMBLE-EXCEEDANCE-1",
        rules_id,
    )


def _deriv(instrument: str) -> tuple[Market, ...]:
    base = {"EUR/USD": D("1.0800"), "GBP/USD": D("1.2500"), "USD/JPY": D("149.000")}[
        instrument
    ]
    scale = D("100") if instrument == "USD/JPY" else ONE
    width = D("0.00008") * scale
    currents = []
    for i in range(192):
        currents.append(
            base + scale * (D(i) * D("0.00010") + D(i % 5 - 2) * D("0.00002"))
        )
    trend_top = currents[-1]
    currents.extend(
        trend_top + scale * D(change) for change in ("-0.00116", "-0.00058", "-0.00048")
    )
    center = currents[-1]
    for _ in range(3):
        currents.extend(
            center + scale * D(shape)
            for shape in (
                (
                    "0.00004",
                    "-0.00006",
                    "0.00007",
                    "-0.00003",
                    "0.00002",
                    "-0.00004",
                    "0.00001",
                    "-0.00002",
                    "0.00007",
                    "-0.00001",
                    "0.00006",
                    "-0.00005",
                )
                * 5
                + ("0.00020", "0.00025", "0.00043", "0.00013")
                + ("0.00014", "-0.00013", "0.00001", "-0.00006", "0.00018", "0.00003")
                * 8
                + ("-0.00068", "-0.00009", "-0.00007", "0.00004", "0.00011")
            )
        )
    currents.extend([center] * 12)

    observations, bars, confirmation = [], [], []
    quotes = tuple(
        DerivQuote(side, D("5.00"), D("9.30"), duration)
        for side in ("CALL", "PUT")
        for duration in (300, 600, 900)
    )
    for i, close in enumerate(currents):
        timestamp = START + (i + 1) * 300
        opening = bars[-1].close if bars else close
        bars.append(
            Candle(
                timestamp,
                opening,
                max(opening, close) + width,
                min(opening, close) - width,
                close,
            )
        )
        if (i + 1) % 3 == 0:
            window = bars[-3:]
            confirmation.append(
                Candle(
                    timestamp,
                    window[0].open,
                    max(bar.high for bar in window),
                    min(bar.low for bar in window),
                    close,
                )
            )
        observations.append(
            Market(
                "DERIV",
                instrument,
                instrument,
                timestamp,
                SOURCE,
                tuple(bars[-160:]),
                tuple(confirmation[-120:]),
                contracts=quotes,
            )
        )
    return tuple(observations)


def _book(bid: str, ask: str, depth: str = "15") -> Book:
    return Book(D(bid), D(ask), D(depth), D(depth), fee_schedule_verified=True)


def _kalshi(instrument: str) -> tuple[Market, ...]:
    observations = []
    previous = D("0.50")
    bars: list[Candle] = []
    books = (
        ("0.44", "0.56", "0"),
        ("0.41", "0.45", "2"),
        ("0.43", "0.51", "0"),
        ("0.48", "0.59", "0"),
        ("0.43", "0.53", "3"),
        ("0.47", "0.59", "0"),
        ("0.48", "0.56", "2"),
        ("0.47", "0.52", "0"),
        ("0.45", "0.52", "1"),
        ("0.46", "0.49", "0"),
        ("0.45", "0.53", "0"),
        ("0.46", "0.53", "0"),
    )
    for event_index in range(6):
        issued = START + event_index * 900
        closes_at = issued + len(books) * 60
        event = f"FIXTURE-TEMP-NYC-DAY-{event_index + 1:02}"
        rules_id = f"synthetic-knyc-daily-high-strict-over-73-{event_index + 1}"
        forecast = ensemble_exceedance(
            tuple(D(70 + (member % 9)) + D("3.6") for member in range(54)),
            D(73),
            issued_at=issued,
            expires_at=closes_at,
            rules_id=rules_id,
            source="simulated-ensemble",
        )
        for step, (bid, ask, sell_volume) in enumerate(books):
            timestamp = issued + step * 60
            book = _book(bid, ask)
            close = (book.yes_bid + book.yes_ask) / 2
            bars.append(
                Candle(
                    timestamp,
                    previous,
                    max(previous, close),
                    min(previous, close),
                    close,
                    D(sell_volume),
                )
            )
            previous = close
            relation = Relation(
                other_instrument="FIXTURE-TEMP-NYC-71",
                kind="PRIMARY_SUBSET",
                verified=True,
                other_book=_book("0.19", "0.24"),
                observed_at=timestamp,
                rules_id=rules_id,
            )
            observations.append(
                Market(
                    "KALSHI",
                    instrument,
                    event,
                    timestamp,
                    SOURCE,
                    tuple(bars),
                    book=book,
                    forecast=forecast,
                    relation=relation,
                    closes_at=closes_at,
                    observed_sell_volume=D(sell_volume),
                    rules_id=rules_id,
                    granularity=60,
                )
            )
        observed_temperature = D(71 if event_index % 3 == 2 else 76)
        observations.append(
            Market(
                "KALSHI",
                instrument,
                event,
                closes_at,
                SOURCE,
                tuple(bars),
                outcome=observed_temperature > D(73),
                closes_at=closes_at,
                rules_id=rules_id,
                granularity=60,
            )
        )
    return tuple(observations)


@lru_cache(maxsize=8)
def dataset(exchange: str, instrument: str | None = None) -> tuple[Market, ...]:
    venue = exchange.upper()
    name = instrument or DEFAULT_INSTRUMENT.get(venue)
    if name not in INSTRUMENTS.get(venue, ()):
        raise ValueError("No offline dataset for this exchange and instrument")
    return _deriv(name) if venue == "DERIV" else _kalshi(name)
