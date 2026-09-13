"""The harness that reproduces the README. If this drifts, the README is wrong."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recon.evaluation.harness import as_markdown, run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "transactions.json").exists(), reason="run `make corpus` first"
)


@pytest.fixture(scope="module")
def numbers() -> dict[str, Any]:
    return run(FIXTURES).numbers


def test_it_reports_every_section_the_readme_quotes(numbers: dict[str, Any]) -> None:
    assert set(numbers) == {
        "corpus",
        "layers",
        "matching",
        "calibration",
        "threshold",
        "extraction",
        "adversarial",
        "review_queue",
        "against_doing_it_by_hand",
    }


def test_the_layer_shares_add_up_to_one(numbers: dict[str, Any]) -> None:
    shares = [row["share"] for row in numbers["layers"]["per_rule"].values()]
    assert sum(shares) == pytest.approx(1.0, abs=0.001)


def test_deterministic_still_does_most_of_the_work(numbers: dict[str, Any]) -> None:
    """The architecture's whole claim. If the model overtakes the rules, a rule
    is missing rather than the model being clever."""
    summary = numbers["layers"]["summary"]
    assert summary["deterministic"] > summary["probabilistic"] * 4


def test_no_layer_that_fired_was_ever_wrong(numbers: dict[str, Any]) -> None:
    for name, row in numbers["layers"]["per_rule"].items():
        if row["precision"] is not None:
            assert row["precision"] == 1.0, f"{name} got {row['correct']}/{row['resolved']}"


def test_precision_and_recall_are_both_reported(numbers: dict[str, Any]) -> None:
    matching = numbers["matching"]
    assert matching["precision"] >= 0.98
    assert matching["recall"] >= 0.7
    assert matching["wrong_auto_clears"] == 0


def test_calibration_is_measured_on_held_out_data(numbers: dict[str, Any]) -> None:
    calibration = numbers["calibration"]
    assert calibration["held_out_rows"] > 100
    assert calibration["held_out_brier"] < 0.06
    assert calibration["expected_calibration_error"] < 0.08
    assert calibration["reliability"], "the reliability diagram has to be in there"


def test_the_threshold_shows_its_working(numbers: dict[str, Any]) -> None:
    threshold = numbers["threshold"]
    assert 0.5 <= threshold["line"] <= 0.99
    assert "ratio" in threshold["cost_matrix"]
    assert "minutes" in threshold["cost_matrix"]["assumption"]


def test_extraction_is_scored_separately_and_honestly(numbers: dict[str, Any]) -> None:
    extraction = numbers["extraction"]
    assert extraction["all"]["wrongly_read"] == 0
    assert extraction["written_after_the_parser_was_finished"]["reports"] >= 10
    assert "has not failed" in extraction["caveat"]


def test_the_adversarial_slice_is_reported_and_clean(numbers: dict[str, Any]) -> None:
    """Duplicates, strangers' money, and invoices that fit equally well. These
    vanish into an average, so they get their own section."""
    adversarial = numbers["adversarial"]
    assert {"duplicate_submission", "no_matching_order", "ambiguous_twin_invoices"} <= set(
        adversarial
    )
    assert adversarial["verdict"] == "no adversarial case was closed wrongly"


def test_no_duplicate_or_stranger_payment_is_ever_closed_unattended(
    numbers: dict[str, Any],
) -> None:
    for case in ("duplicate_submission", "no_matching_order"):
        assert numbers["adversarial"][case]["closed_unattended"] == 0


def test_an_evening_of_reviewing_covers_most_of_the_money(numbers: dict[str, Any]) -> None:
    queue = numbers["review_queue"]
    assert queue["an_evening_is"] == 40
    assert queue["share_of_the_money_in_that_evening"] > 0.7


def test_it_compares_against_doing_it_by_hand(numbers: dict[str, Any]) -> None:
    versus = numbers["against_doing_it_by_hand"]
    assert versus["payments_in_the_month"] > 300
    assert (
        versus["if_a_person_checked_every_one"]["hours"]
        > versus["with_this_running"]["hours_of_review"]
    )
    assert "pessimistic" in versus["caveat"]


def test_the_markdown_table_is_ready_to_paste(numbers: dict[str, Any]) -> None:
    table = as_markdown(numbers)
    assert table.startswith("| Layer |")
    assert "exact reference" in table
    assert "sent to a person" in table


def test_running_it_twice_gives_the_same_numbers() -> None:
    """The README quotes these. They must not wander between runs."""
    first = run(FIXTURES).numbers
    second = run(FIXTURES).numbers
    assert first["layers"] == second["layers"]
    assert first["matching"] == second["matching"]
    assert first["threshold"]["line"] == second["threshold"]["line"]
