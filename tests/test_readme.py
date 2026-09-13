"""The README quotes numbers. This checks it is quoting the real ones.

The rule for this repo is that nothing goes in the README that `make eval`
cannot regenerate. Without a test, that rule lasts about two commits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from recon.evaluation.harness import run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
README = (ROOT / "README.md").read_text()

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "transactions.json").exists(), reason="run `make corpus` first"
)


@pytest.fixture(scope="module")
def numbers():  # type: ignore[no-untyped-def]
    return run(FIXTURES).numbers


def test_the_headline_coverage_matches(numbers) -> None:  # type: ignore[no-untyped-def]
    rate = numbers["matching"]["auto_clear_rate"]
    assert f"{rate:.1%}" in README, f"README should say {rate:.1%} somewhere"


def test_the_layer_split_matches(numbers) -> None:  # type: ignore[no-untyped-def]
    summary = numbers["layers"]["summary"]
    assert f"{summary['deterministic']:.1%}" in README
    assert f"{summary['probabilistic']:.1%}" in README


def test_the_payment_count_and_total_match(numbers) -> None:  # type: ignore[no-untyped-def]
    assert str(numbers["corpus"]["payments"]) in README
    assert numbers["corpus"]["money"] in README


def test_the_threshold_matches(numbers) -> None:  # type: ignore[no-untyped-def]
    assert str(numbers["threshold"]["line"]) in README


def test_the_cost_matrix_matches(numbers) -> None:  # type: ignore[no-untyped-def]
    costs = numbers["threshold"]["cost_matrix"]
    assert costs["a review costs"].replace(".00", "") in README
    assert costs["a wrong auto-clear costs"].replace(".00", "") in README
    assert costs["ratio"].replace(":1", " to 1") in README


def test_the_calibration_numbers_match(numbers) -> None:  # type: ignore[no-untyped-def]
    calibration = numbers["calibration"]
    assert f"{calibration['held_out_brier']:.4f}" in README
    assert str(calibration["held_out_rows"]) in README


def test_the_review_queue_numbers_match(numbers) -> None:  # type: ignore[no-untyped-def]
    queue = numbers["review_queue"]
    assert str(queue["waiting"]) in README
    assert f"{queue['share_of_the_money_in_that_evening']:.0%}" in README


def test_the_saving_against_doing_it_by_hand_matches(numbers) -> None:  # type: ignore[no-untyped-def]
    versus = numbers["against_doing_it_by_hand"]
    assert versus["saved"].replace(".00", "") in README
    assert str(versus["if_a_person_checked_every_one"]["hours"]) in README


def test_the_extraction_numbers_match(numbers) -> None:  # type: ignore[no-untyped-def]
    extraction = numbers["extraction"]["all"]
    assert extraction["read_completely_right"] in README
    assert str(extraction["reports"]) in README


def test_the_layer_table_rows_match(numbers) -> None:  # type: ignore[no-untyped-def]
    for name, row in numbers["layers"]["per_rule"].items():
        if name == "sent to a person":
            continue
        assert row["money"] in README, f"{name}'s money is stale in the README"
        assert f"{row['share']:.1%}" in README


def test_it_says_what_it_cannot_do(numbers) -> None:  # type: ignore[no-untyped-def]
    """The limitations have to be specific and in the present tense.

    "Future work could include better cash handling" is a way of not saying
    anything. "Cash cannot be verified and nothing can fix that" is a limitation.
    """
    section = README[README.index("## What it does not do") :]

    claims = [line for line in section.splitlines() if line.startswith("**")]
    assert len(claims) >= 8, "a limitations section with fewer than eight lines is a gesture"
    for claim in claims:
        assert not claim.lower().startswith("**future"), claim

    for must_mention in ("cash", "simulation", "retrain", "docker", "87 payments"):
        assert must_mention in section.lower(), f"the limitations skip {must_mention}"


def test_no_placeholder_numbers_are_left_in() -> None:
    assert not re.search(r"\bTODO\b|\bXX%|\bNN\b", README)
