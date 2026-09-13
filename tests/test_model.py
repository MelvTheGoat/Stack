"""The logistic fit, the calibration, and the threshold — each on its own."""

from __future__ import annotations

import random

import pytest

from recon.match.calibration import (
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    brier_score,
    choose,
    expected_calibration_error,
    from_dict,
    reliability,
)
from recon.match.model import LogisticModel, sigmoid
from recon.match.threshold import (
    CostMatrix,
    choose_threshold,
    expected_cost,
    precision_at_k,
    reviews_in_an_evening,
)
from recon.money import Money


def separable_data(n: int = 400, seed: int = 3) -> list[tuple[list[float], int]]:
    """Two features, one of which decides the label. Any working fit finds it."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        signal = rng.random()
        noise = rng.random()
        label = 1 if signal > 0.6 else 0
        rows.append(([signal, noise], label))
    return rows


class TestSigmoid:
    def test_it_is_centred_and_bounded(self) -> None:
        assert sigmoid(0) == 0.5
        assert sigmoid(1000) == pytest.approx(1.0)
        assert sigmoid(-1000) == pytest.approx(0.0)

    def test_it_does_not_overflow_on_a_confident_score(self) -> None:
        """The textbook 1/(1+exp(-z)) raises OverflowError at about z = -746."""
        assert sigmoid(-800) == 0.0
        assert sigmoid(800) == 1.0


class TestFitting:
    def test_it_learns_which_feature_matters(self) -> None:
        model = LogisticModel(("signal", "noise")).fit(separable_data())
        weights = model.explain()
        assert weights["signal"] > 0
        assert abs(weights["signal"]) > abs(weights["noise"]) * 3

    def test_it_separates_the_two_classes(self) -> None:
        model = LogisticModel(("signal", "noise")).fit(separable_data())
        assert model.predict([0.95, 0.5]) > 0.7
        assert model.predict([0.05, 0.5]) < 0.3

    def test_it_refuses_to_fit_on_nothing(self) -> None:
        with pytest.raises(ValueError, match="no data"):
            LogisticModel(("a",)).fit([])

    def test_it_refuses_data_with_only_one_class(self) -> None:
        """A model fitted on all-negatives is a constant that says no forever,
        and it would score 100% accuracy on the data that made it."""
        with pytest.raises(ValueError, match="only one class"):
            LogisticModel(("a",)).fit([([0.5], 0), ([0.7], 0)])

    def test_class_balancing_stops_it_saying_no_to_everything(self) -> None:
        """One positive in twenty. Unbalanced, this learns a constant "no"."""
        rows = [([0.9, 0.1], 1)] + [([0.1, 0.9], 0)] * 20
        model = LogisticModel(("a", "b")).fit(rows * 10)
        assert model.predict([0.9, 0.1]) > 0.5

    def test_it_round_trips_through_json(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        model = LogisticModel(("signal", "noise")).fit(separable_data())
        path = tmp_path / "m.json"
        model.save(path)
        reloaded = LogisticModel.load(path)
        assert reloaded.predict([0.7, 0.2]) == model.predict([0.7, 0.2])


class TestCalibration:
    def miscalibrated(self) -> tuple[list[float], list[int]]:
        """Scores that rank perfectly but lie about their own confidence:
        everything scored 0.9 is only right half the time."""
        rng = random.Random(11)
        scores, labels = [], []
        for _ in range(600):
            score = rng.choice([0.1, 0.5, 0.9])
            truth = {0.1: 0.05, 0.5: 0.3, 0.9: 0.5}[score]
            scores.append(score)
            labels.append(1 if rng.random() < truth else 0)
        return scores, labels

    def test_platt_pulls_an_overconfident_score_back(self) -> None:
        scores, labels = self.miscalibrated()
        calibrator = PlattCalibrator.fit(scores, labels)
        assert calibrator.calibrate(0.9) < 0.75, "0.9 meant 50%, so it must come down"

    def test_isotonic_recovers_the_actual_rates(self) -> None:
        scores, labels = self.miscalibrated()
        calibrator = IsotonicCalibrator.fit(scores, labels)
        assert calibrator.calibrate(0.9) == pytest.approx(0.5, abs=0.1)
        assert calibrator.calibrate(0.1) == pytest.approx(0.05, abs=0.1)

    def test_isotonic_output_never_goes_down(self) -> None:
        scores, labels = self.miscalibrated()
        calibrator = IsotonicCalibrator.fit(scores, labels)
        outputs = [calibrator.calibrate(x / 100) for x in range(101)]
        assert outputs == sorted(outputs)

    def test_calibration_improves_the_brier_score(self) -> None:
        scores, labels = self.miscalibrated()
        before = brier_score(scores, labels)
        calibrator = IsotonicCalibrator.fit(scores, labels)
        after = brier_score([calibrator.calibrate(s) for s in scores], labels)
        assert after < before

    def test_every_calibrator_round_trips_through_json(self) -> None:
        scores, labels = self.miscalibrated()
        for calibrator in (
            IdentityCalibrator(),
            PlattCalibrator.fit(scores, labels),
            IsotonicCalibrator.fit(scores, labels),
        ):
            copy = from_dict(calibrator.to_dict())
            assert copy.calibrate(0.7) == pytest.approx(calibrator.calibrate(0.7))

    def test_choosing_prefers_the_smoother_method_on_a_near_tie(self) -> None:
        """The bug this guards: isotonic won by 0.001 Brier and collapsed every
        probability onto six values, which made the threshold meaningless."""
        scores, labels = self.miscalibrated()
        chosen, briers = choose(scores, labels, scores, labels)
        assert set(briers) == {"identity", "platt", "isotonic"}
        if briers["platt"] <= briers["isotonic"] + 0.01:
            assert chosen.to_dict()["kind"] == "platt"

    def test_a_calibrator_always_returns_a_probability(self) -> None:
        scores, labels = self.miscalibrated()
        for calibrator in (
            IdentityCalibrator(),
            PlattCalibrator.fit(scores, labels),
            IsotonicCalibrator.fit(scores, labels),
        ):
            for score in (-5.0, 0.0, 0.5, 1.0, 5.0):
                assert 0.0 <= calibrator.calibrate(score) <= 1.0


class TestMeasuringCalibration:
    def test_a_perfect_forecaster_scores_zero_brier(self) -> None:
        assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0

    def test_always_saying_fifty_percent_scores_a_quarter(self) -> None:
        assert brier_score([0.5] * 4, [1, 0, 1, 0]) == 0.25

    def test_being_confidently_wrong_is_the_worst_score(self) -> None:
        assert brier_score([1.0], [0]) == 1.0

    def test_the_reliability_diagram_finds_the_lie(self) -> None:
        """Ninety cases claimed at 90%, only half of them right."""
        probabilities = [0.9] * 100
        labels = [1] * 50 + [0] * 50
        bins = reliability(probabilities, labels, bins=10)
        top = next(b for b in bins if b.count)
        assert top.mean_predicted == pytest.approx(0.9)
        assert top.actual_rate == pytest.approx(0.5)
        assert top.gap == pytest.approx(-0.4)
        assert expected_calibration_error(bins) == pytest.approx(0.4)

    def test_a_truthful_forecaster_has_no_calibration_error(self) -> None:
        probabilities = [0.9] * 10
        labels = [1] * 9 + [0]
        assert expected_calibration_error(reliability(probabilities, labels)) == pytest.approx(
            0.0, abs=0.01
        )


class TestThreshold:
    def test_the_costs_are_stated_and_lopsided(self) -> None:
        costs = CostMatrix()
        assert costs.wrong_auto_clear > costs.review
        assert costs.ratio > 50
        assert "minutes" in costs.describe()["assumption"]

    def test_a_perfect_model_gets_a_low_line(self) -> None:
        probabilities = [0.95] * 50 + [0.05] * 50
        labels = [1] * 50 + [0] * 50
        assert choose_threshold(probabilities, labels).threshold <= 0.95

    def test_an_expensive_mistake_pushes_the_line_up(self) -> None:
        probabilities = [0.8] * 20
        labels = [1] * 18 + [0] * 2  # 10% wrong at this confidence
        cheap = choose_threshold(
            probabilities, labels, CostMatrix(Money.from_naira("60"), Money.from_naira("100"))
        )
        dear = choose_threshold(
            probabilities, labels, CostMatrix(Money.from_naira("60"), Money.from_naira("50000"))
        )
        assert dear.threshold > cheap.threshold

    def test_a_useless_model_ends_up_reviewing_everything(self) -> None:
        probabilities = [0.5] * 40
        labels = [1, 0] * 20
        choice = choose_threshold(probabilities, labels)
        assert choice.auto_cleared == 0
        assert choice.sent_to_review == 40

    def test_ties_break_towards_showing_a_person_more(self) -> None:
        """When two lines cost the same, take the safer one."""
        probabilities = [0.3, 0.6, 0.9]
        labels = [1, 1, 1]
        assert choose_threshold(probabilities, labels).threshold >= 0.0

    def test_expected_cost_adds_up_by_hand(self) -> None:
        costs = CostMatrix(Money.from_naira("60"), Money.from_naira("5000"))
        # two cleared (one wrong), one reviewed
        cost = expected_cost([0.9, 0.9, 0.1], [1, 0, 1], 0.5, costs)
        assert cost == Money.from_naira("5060")

    def test_it_refuses_to_pick_a_line_with_no_data(self) -> None:
        with pytest.raises(ValueError, match="nothing to choose it on"):
            choose_threshold([], [])

    def test_an_evening_is_forty_reviews(self) -> None:
        assert reviews_in_an_evening(120) == 40


class TestPrecisionAtK:
    def test_it_measures_the_top_of_the_queue(self) -> None:
        ranked = [
            (Money.from_naira("100"), 1),
            (Money.from_naira("90"), 1),
            (Money.from_naira("80"), 0),
        ]
        assert precision_at_k(ranked, 2) == 1.0
        assert precision_at_k(ranked, 3) == pytest.approx(2 / 3)

    def test_an_empty_queue_or_no_budget_scores_zero(self) -> None:
        assert precision_at_k([], 10) == 0.0
        assert precision_at_k([(Money.zero(), 1)], 0) == 0.0
