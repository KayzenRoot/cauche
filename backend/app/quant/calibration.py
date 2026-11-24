"""Deterministic probability research using matured point-in-time observations."""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import exp, sqrt

from .types import Market

_MIN_TRAIN = 24
_MIN_VALIDATION = 12


@dataclass(frozen=True)
class Sample:
    observed_at: int
    matured_at: int
    key: tuple[str, str]
    features: tuple[float, ...]
    outcome: bool
    reference: float | None = None


def deriv_features(market: Market, side: str) -> tuple[float, ...] | None:
    bars = tuple(bar for bar in market.candles if bar.closed and bar.time <= market.timestamp)[-13:]
    if len(bars) < 13 or not all(bar.valid() for bar in bars):
        return None
    if any(right.time - left.time != market.granularity for left, right in zip(bars, bars[1:])):
        return None
    if bars[-1].time != market.timestamp:
        return None
    sign = 1 if side == "CALL" else -1
    returns = [(bars[-1].close / bars[-1 - step].close) - 1 for step in (1, 3, 12)]
    distance = (max(bar.high for bar in bars) - min(bar.low for bar in bars)) / bars[-1].close
    return tuple(float(value * sign) for value in returns) + (float(distance), float(sign))


def kalshi_features(market: Market) -> tuple[float, ...] | None:
    book = market.book
    if book is None or not book.valid():
        return None
    midpoint = (book.yes_bid + book.yes_ask) / 2
    imbalance = (book.yes_bid_size - book.yes_ask_size) / (book.yes_bid_size + book.yes_ask_size)
    forecast = market.forecast
    independent = forecast.probability if forecast is not None and forecast.is_usable(market) else midpoint
    return tuple(float(value) for value in (midpoint, book.spread, imbalance, independent))


def samples(markets: Sequence[Market], duration: int) -> tuple[Sample, ...]:
    """Build labels separately from the features available at observation time."""
    result = []
    if markets and markets[0].exchange == "KALSHI":
        resolved = {
            market.event: market for market in markets
            if market.outcome is not None and market.closes_at is not None
            and market.timestamp >= market.closes_at
        }
        evaluated = set()
        for market in markets:
            resolution = resolved.get(market.event)
            features = kalshi_features(market)
            if (
                market.event in evaluated or resolution is None or features is None
                or market.outcome is not None or market.closes_at is None
                or market.timestamp >= market.closes_at
                or resolution.timestamp <= market.timestamp
                or market.rules_id != resolution.rules_id
            ):
                continue
            evaluated.add(market.event)
            result.append(Sample(
                market.timestamp, resolution.timestamp, (market.event, "YES"),
                features, bool(resolution.outcome), features[0],
            ))
        return tuple(result)

    last_bars = {}
    for market in markets:
        if market.candles:
            bar = market.candles[-1]
            if bar.closed and bar.time == market.timestamp and bar.valid():
                last_bars[(market.instrument, bar.time)] = bar
    for market in markets:
        entry_at = market.timestamp + market.granularity
        expiry = entry_at + duration
        if duration <= 0 or duration % market.granularity or (market.closes_at is not None and expiry > market.closes_at):
            continue
        entry = last_bars.get((market.instrument, entry_at))
        closing = last_bars.get((market.instrument, expiry))
        if entry is None or closing is None:
            continue
        for side in ("CALL", "PUT"):
            features = deriv_features(market, side)
            if features is not None:
                outcome = closing.close > entry.close if side == "CALL" else closing.close < entry.close
                result.append(Sample(
                    market.timestamp, expiry, (market.instrument, side), features, outcome,
                ))
    return tuple(result)


def mature_slice(rows: Sequence[Sample], start: int, end: int) -> tuple[Sample, ...]:
    """Purge labels that are still open at the partition boundary."""
    return tuple(row for row in rows if start <= row.observed_at < row.matured_at <= end)


def _probability(logit: float) -> float:
    return 1 / (1 + exp(-max(-30.0, min(30.0, logit))))


def _logistic(
    features: Sequence[tuple[float, ...]], outcomes: Sequence[bool],
) -> Callable[[tuple[float, ...]], float]:
    columns = list(zip(*features))
    n = len(features)
    means = [sum(column) / n for column in columns]
    scales = [
        sqrt(sum((value - center) ** 2 for value in column) / n) or 1.0
        for center, column in zip(means, columns)
    ]

    def standardized(row: tuple[float, ...]) -> tuple[float, ...]:
        return (1.0,) + tuple(
            max(-8.0, min(8.0, (value - center) / scale))
            for value, center, scale in zip(row, means, scales)
        )

    design = [standardized(row) for row in features]
    weights = [0.0] * (len(columns) + 1)
    for _ in range(96):
        gradient = [0.0] * len(weights)
        for row, target in zip(design, outcomes):
            residual = _probability(sum(weight * value for weight, value in zip(weights, row))) - target
            for index, value in enumerate(row):
                gradient[index] += residual * value
        weights = [
            weight - 0.15 * (change / n + (0.02 * weight if index else 0.0))
            for index, (change, weight) in enumerate(zip(gradient, weights))
        ]

    return lambda row: _probability(sum(
        weight * value for weight, value in zip(weights, standardized(row))
    ))


def _tree(rows: Sequence[Sample], depth: int = 2) -> Callable[[tuple[float, ...]], float]:
    wins = sum(row.outcome for row in rows)
    fallback = (wins + 1) / (len(rows) + 2)
    if depth == 0 or len(rows) < 16 or wins in {0, len(rows)}:
        return lambda _: fallback
    best = None
    for index in range(len(rows[0].features)):
        ordered = sorted(rows, key=lambda row: row.features[index])
        left_wins = 0
        for boundary, row in enumerate(ordered[:-1], start=1):
            left_wins += row.outcome
            right = ordered[boundary].features[index]
            left = row.features[index]
            if min(boundary, len(rows) - boundary) < 8 or left == right:
                continue
            right_wins = wins - left_wins
            impurity = (
                left_wins * (1 - left_wins / boundary)
                + right_wins * (1 - right_wins / (len(rows) - boundary))
            )
            if best is None or impurity < best[0]:
                best = (impurity, index, (left + right) / 2, ordered[:boundary], ordered[boundary:])
    if best is None:
        return lambda _: fallback
    _, index, threshold, left, right = best
    low = _tree(left, depth - 1)
    high = _tree(right, depth - 1)
    return lambda row: (low if row[index] < threshold else high)(row)


def _score(rows: Sequence[Sample], predictions: Sequence[float]) -> dict:
    buckets = defaultdict(list)
    for row, prediction in zip(rows, predictions):
        buckets[min(9, int(prediction * 10))].append((prediction, row.outcome))
    count = len(rows)
    return {
        "observations": count,
        "brier_score": (
            sum((p - row.outcome) ** 2 for row, p in zip(rows, predictions)) / count
            if count else None
        ),
        "calibration_bins": [
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "observations": len(items),
                "mean_probability": sum(p for p, _ in items) / len(items),
                "observed_rate": sum(outcome for _, outcome in items) / len(items),
            }
            for index, items in sorted(buckets.items())
        ],
    }


def study(train: Sequence[Sample], validation: Sequence[Sample], test: Sequence[Sample]) -> dict:
    """Fit on train and calibrate on validation before scoring the untouched test partition."""
    counts = {"train": len(train), "validation": len(validation), "test": len(test)}
    if not test or not train:
        return {"status": "INSUFFICIENT_DATA", "sample_counts": counts, "models": []}
    rates = defaultdict(list)
    for row in train:
        rates[row.key[1]].append(row.outcome)

    def reference(row: Sample) -> float:
        if row.reference is not None:
            return row.reference
        observed = rates[row.key[1]]
        return (sum(observed) + 1) / (len(observed) + 2)

    reference_model = {
        "model": "MARKET_MIDPOINT" if test[0].reference is not None else "TRAIN_BASE_RATE",
        "method": "REFERENCE",
        "validation": _score(validation, [reference(row) for row in validation]),
        "evaluation": _score(test, [reference(row) for row in test]),
    }
    trains_two_classes = len({row.outcome for row in train}) == 2
    validation_two_classes = len({row.outcome for row in validation}) == 2
    if len(train) < _MIN_TRAIN or len(validation) < _MIN_VALIDATION or not (trains_two_classes and validation_two_classes):
        return {"status": "INSUFFICIENT_MODEL_DATA", "sample_counts": counts, "models": [reference_model]}

    logistic = _logistic([row.features for row in train], [row.outcome for row in train])
    models = [reference_model]
    for name, predict in (("LOGISTIC_REGRESSION", logistic), ("SHALLOW_TREE", _tree(train))):
        probabilities = [(predict(row.features),) for row in validation]
        calibrate = _logistic(probabilities, [row.outcome for row in validation])
        models.append({
            "model": name,
            "method": "SIGMOID_CALIBRATED",
            "validation": _score(validation, [probability[0] for probability in probabilities]),
            "evaluation": _score(test, [calibrate((predict(row.features),)) for row in test]),
        })
    return {"status": "EVALUATED_SHADOW_ONLY", "sample_counts": counts, "models": models}
