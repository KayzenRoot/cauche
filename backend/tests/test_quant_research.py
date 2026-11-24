from dataclasses import replace
from decimal import Decimal

from app.domain import backtest, dataset
from app.quant.calibration import Sample, mature_slice, samples, study
from app.quant.research import walk_forward


def test_mature_labels_are_purged_at_partition_boundary():
    observations = (
        Sample(4, 8, ("fixture", "CALL"), (0.0,), True),
        Sample(6, 11, ("fixture", "CALL"), (0.1,), False),
        Sample(11, 13, ("fixture", "CALL"), (0.2,), True),
    )
    assert mature_slice(observations, 0, 10) == observations[:1]
    assert mature_slice(observations, 11, 20) == observations[-1:]


def test_kalshi_calibration_uses_one_settlement_each():
    labels = samples(dataset("KALSHI"), 0)
    assert len(labels) == 6
    assert len({label.key[0] for label in labels}) == 6
    assert all(label.reference is not None and label.observed_at < label.matured_at for label in labels)


def test_interleaved_event_observations_stay_in_the_same_partition():
    markets = list(dataset("KALSHI"))
    # Extend the first event through the start of the second event.
    markets.append(replace(markets[0], timestamp=markets[14].timestamp + 1))
    markets.sort(key=lambda market: market.timestamp)
    windows = walk_forward(markets)
    assert windows
    for window in windows:
        segments = [
            {market.event for market in markets[start:end]}
            for start, end in (window.train, window.validation, window.test)
        ]
        assert len(set.union(*segments)) == sum(map(len, segments))


def test_logistic_tree_and_reference_are_scored_without_fitting_to_test_labels():
    def row(index: int) -> Sample:
        feature = (index % 8) / 7
        return Sample(index, index + 1, ("fixture", "YES"), (feature,), feature >= 0.5, 0.5)

    train = tuple(row(index) for index in range(48))
    validation = tuple(row(index) for index in range(50, 74))
    test = tuple(row(index) for index in range(80, 104))
    flipped = tuple(replace(item, outcome=not item.outcome) for item in test)
    report = study(train, validation, test)
    other = study(train, validation, flipped)
    assert report["status"] == "EVALUATED_SHADOW_ONLY"
    assert {model["model"] for model in report["models"]} == {
        "MARKET_MIDPOINT", "LOGISTIC_REGRESSION", "SHALLOW_TREE",
    }
    assert report["models"][0]["evaluation"]["brier_score"] == 0.25
    for original, changed in zip(report["models"], other["models"]):
        assert original["validation"] == changed["validation"]
        assert [
            bin["mean_probability"] for bin in original["evaluation"]["calibration_bins"]
        ] == [bin["mean_probability"] for bin in changed["evaluation"]["calibration_bins"]]
    for model in report["models"][1:]:
        assert model["evaluation"]["brier_score"] < 0.25


def test_backtest_contains_frozen_holdout_and_reconciled_execution_receipts():
    result = backtest("KALSHI", "KALSHI-STRAT-02")
    execution = result["execution"]
    assert execution["submitted"] == result["intent_count"] == sum(execution["outcomes"].values())
    assert execution["filled"] >= execution["partial"] > 0
    assert Decimal(execution["average_fill_latency_seconds"]) > 0
    assert all(
        Decimal(row["requested_quantity"]) >= Decimal(row["filled_quantity"])
        for row in execution["receipts"]
    )
    research = result["research"]
    walk = research["walk_forward"]
    holdout = walk["holdout"]
    assert walk["candidate_count"] == 2
    assert len(walk["windows"]) == 2
    assert all(window["test"]["end"] < holdout["test"]["start"] for window in walk["windows"])
    assert holdout["train"]["end"] < holdout["validation"]["start"] < holdout["test"]["start"]
    assert holdout["test_metrics"] == holdout["shadow_candidates"][holdout["selected_candidate"]]
    assert research["calibration"]["status"] == "INSUFFICIENT_MODEL_DATA"
    assert research["shadow_status"] == "RESEARCH_ONLY_NO_PROMOTION"
