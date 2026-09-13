"""The baseline, pinned.

These are the deterministic-layer numbers the README quotes. If a change moves
them, this test fails and the README has to be updated in the same commit. That
is the point of pinning them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.enums import Layer
from recon.evaluation import truth as truth_module
from recon.evaluation.score import score
from recon.match.deterministic import DeterministicMatcher
from recon.match.ledger import Ledger, transactions_from_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "transactions.json").exists(), reason="run `make corpus` first"
)


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    ledger = Ledger.from_fixtures(FIXTURES)
    transactions = transactions_from_fixtures(FIXTURES)
    matches = DeterministicMatcher(ledger).match_all(transactions)
    answers = truth_module.load(FIXTURES / "ground_truth.json")
    return score(matches, answers), matches


def test_it_closes_about_seventy_percent_of_the_month(report) -> None:  # type: ignore[no-untyped-def]
    result, _ = report
    assert 0.66 <= result.auto_clear_rate <= 0.74, (
        f"deterministic coverage moved to {result.auto_clear_rate:.1%}; "
        "update the README table in this commit"
    )


def test_when_it_answers_it_is_never_wrong(report) -> None:  # type: ignore[no-untyped-def]
    """The whole justification for a deterministic first layer.

    If this drops below 1.0, one of the rules has started guessing and needs to
    be moved down into the probabilistic layer where guesses are scored.
    """
    result, _ = report
    assert result.auto_precision == 1.0, {
        layer: (s.correct, s.decided) for layer, s in result.by_layer.items()
    }


def test_the_residual_is_the_hard_cases_and_not_the_easy_ones(report) -> None:  # type: ignore[no-untyped-def]
    _, matches = report
    answers = truth_module.load(FIXTURES / "ground_truth.json")
    left_over = [m for m in matches if m.layer is Layer.UNRESOLVED]
    cases = {answers[m.transaction_reference].case for m in left_over}

    assert "card_with_reference" not in cases, "the easiest case must never survive layer one"
    assert {"transfer_name_partial", "split_three_invoices", "ambiguous_twin_invoices"} <= cases


def test_every_layer_that_fired_was_perfect(report) -> None:  # type: ignore[no-untyped-def]
    result, _ = report
    for layer, layer_score in result.by_layer.items():
        assert layer_score.precision == 1.0, (
            f"{layer} got {layer_score.correct}/{layer_score.decided}"
        )


def test_the_twin_invoices_all_went_to_a_human(report) -> None:  # type: ignore[no-untyped-def]
    _, matches = report
    answers = truth_module.load(FIXTURES / "ground_truth.json")
    twins = [
        m for m in matches if answers[m.transaction_reference].case == "ambiguous_twin_invoices"
    ]
    assert twins
    assert all(not m.resolved for m in twins), "a coin flip is not a match"


def test_most_of_the_money_is_already_settled_by_layer_one(report) -> None:  # type: ignore[no-untyped-def]
    result, _ = report
    share = result.money_auto.kobo / result.money_total.kobo
    assert share > 0.55, f"only {share:.1%} of the money closed deterministically"
