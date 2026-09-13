"""Both layers, on the real corpus. These are the README's headline numbers."""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.evaluation import truth as truth_module
from recon.evaluation.score import score
from recon.match.ledger import Ledger, transactions_from_fixtures
from recon.match.pipeline import Pipeline, layer_mix
from recon.match.training import residual_rows, split_by_time, train

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "transactions.json").exists(), reason="run `make corpus` first"
)


@pytest.fixture(scope="module")
def trained_pipeline():  # type: ignore[no-untyped-def]
    ledger = Ledger.from_fixtures(FIXTURES)
    transactions = transactions_from_fixtures(FIXTURES)
    answers = truth_module.load(FIXTURES / "ground_truth.json")
    trained = train(ledger, transactions, answers)
    matches = Pipeline.build(ledger, trained.build(ledger)).run(transactions)
    return trained, matches, answers


def test_the_layers_stay_in_their_lane(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    """Deterministic first, probabilistic second. If the model starts doing
    layer one's work, a rule has gone missing and this catches the drift."""
    _, matches, _ = trained_pipeline
    mix = layer_mix(matches)
    assert mix["deterministic"] > 0.65
    assert mix["deterministic"] > mix["probabilistic"] * 4
    assert sum(mix.values()) == pytest.approx(1.0)


def test_it_closes_about_three_quarters_of_the_month_unattended(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    _, matches, answers = trained_pipeline
    report = score(matches, answers)
    assert 0.70 <= report.auto_clear_rate <= 0.82, (
        f"auto-clear moved to {report.auto_clear_rate:.1%}; update the README in this commit"
    )


def test_what_it_clears_it_gets_right(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    """The number that actually matters. A wrong auto-clear is money in the
    wrong customer's account and nobody finds out until they complain."""
    _, matches, answers = trained_pipeline
    report = score(matches, answers)
    assert report.auto_precision >= 0.98, {
        layer: (s.correct, s.decided) for layer, s in report.by_layer.items()
    }


def test_the_threshold_came_from_the_cost_matrix_not_a_round_number(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    trained, _, _ = trained_pipeline
    assert 0.5 <= trained.threshold.threshold <= 0.99
    assert trained.threshold.saving_against_manual.kobo > 0, (
        "if the threshold does not beat a person checking everything, "
        "the honest thing is to say so rather than ship it"
    )


def test_the_calibration_was_actually_chosen_between_methods(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    trained, _, _ = trained_pipeline
    assert set(trained.brier_by_method) == {"identity", "platt", "isotonic"}
    chosen = str(trained.calibrator.to_dict()["kind"])
    assert trained.brier_by_method[chosen] <= min(trained.brier_by_method.values()) + 0.01


def test_calibration_holds_up_on_payments_it_never_trained_on(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    from recon.match.calibration import brier_score, expected_calibration_error, reliability

    trained, _, answers = trained_pipeline
    ledger = Ledger.from_fixtures(FIXTURES)
    rows = residual_rows(ledger, transactions_from_fixtures(FIXTURES), answers)
    _, held_out = split_by_time(rows)
    assert held_out, "the split left nothing to test on"

    probabilities = [
        trained.calibrator.calibrate(trained.model.predict(row.vector)) for row in held_out
    ]
    labels = [row.label for row in held_out]

    assert brier_score(probabilities, labels) < 0.06
    assert expected_calibration_error(reliability(probabilities, labels)) < 0.08


def test_the_model_learned_the_things_a_bookkeeper_would_look_at(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    trained, _, _ = trained_pipeline
    weights = trained.model.explain()
    top = list(weights)[:5]
    assert "amount_closeness" in top
    assert "name_similarity" in top


def test_the_queue_is_ranked_by_money_so_the_evening_is_spent_well(trained_pipeline) -> None:  # type: ignore[no-untyped-def]
    from recon.match.threshold import reviews_in_an_evening

    _, matches, _ = trained_pipeline
    queued = sorted((m for m in matches if not m.resolved), key=lambda m: -m.money_at_risk.kobo)
    assert queued
    budget = reviews_in_an_evening()
    top = queued[:budget]
    share = sum(m.money_at_risk.kobo for m in top) / sum(m.money_at_risk.kobo for m in queued)
    assert share > 0.5, (
        f"the first {budget} cases only cover {share:.0%} of the money at risk; "
        "ranking by money is not buying much"
    )
