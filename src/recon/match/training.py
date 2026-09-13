"""Fitting the model, and being honest about how it was measured.

The residual is what the model is for, so the residual is what it trains on:
every payment the deterministic layer could not settle, paired with every
invoice on its shortlist, labelled from the answer key.

Two things here are about honesty rather than accuracy.

**The split is by time, not at random.** Train on the first three weeks, test on
the last. A random split would put a customer's Tuesday payment in training and
their Wednesday payment in test, and the payer-history feature would quietly
leak the answer across. Every number would come out better and mean less.

**Calibration is fitted on its own slice.** Fitting the calibrator on the same
rows the model was fitted on tells you the model is well calibrated on data it
has memorised, which is not a useful thing to know.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from recon.evaluation.truth import Truth
from recon.match.calibration import (
    PREFERENCE,
    TIE_TOLERANCE,
    Calibrator,
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    brier_score,
)
from recon.match.calibration import from_dict as calibration_from_dict
from recon.match.candidates import observed_name
from recon.match.deterministic import DeterministicMatcher
from recon.match.features import FEATURE_NAMES, PayerHistory, as_vector, extract
from recon.match.ledger import Ledger, TxnRow
from recon.match.model import LogisticModel
from recon.match.probabilistic import ProbabilisticMatcher
from recon.match.threshold import CostMatrix, ThresholdChoice, choose_threshold


@dataclass(frozen=True, slots=True)
class TrainingRow:
    transaction_reference: str
    order_references: tuple[str, ...]
    paid_at: datetime
    vector: list[float]
    label: int


@dataclass
class TrainedMatcher:
    model: LogisticModel
    calibrator: Calibrator
    threshold: ThresholdChoice
    brier_by_method: dict[str, float] = field(default_factory=dict)
    rows_used: int = 0
    positives: int = 0
    dev_payments: int = 0
    test_payments: int = 0

    def build(self, ledger: Ledger, history: PayerHistory | None = None) -> ProbabilisticMatcher:
        return ProbabilisticMatcher(
            ledger=ledger,
            model=self.model,
            calibrator=self.calibrator,
            threshold=self.threshold.threshold,
            history=history or PayerHistory(),
        )


def residual_rows(
    ledger: Ledger, transactions: list[TxnRow], truth: dict[str, Truth]
) -> list[TrainingRow]:
    """Build labelled (payment, candidate) pairs out of the deterministic residual."""
    from recon.enums import Layer
    from recon.match.candidates import shortlist

    ordered = sorted(transactions, key=lambda t: (t.paid_at, t.reference))
    by_reference = {t.reference: t for t in ordered}

    matcher = DeterministicMatcher(ledger)
    first_pass = matcher.match_all(ordered)
    claimed = {ref for match in first_pass for ref in match.order_references}

    history = PayerHistory()
    rows: list[TrainingRow] = []

    for match in first_pass:
        if match.layer is not Layer.UNRESOLVED:
            continue
        txn = by_reference[match.transaction_reference]
        answer = truth[txn.reference]
        candidates = shortlist(ledger, txn, claimed)
        if not candidates:
            continue

        open_counts = {
            candidate.customer_id: len(
                [
                    order
                    for order in ledger.orders_for_customer(candidate.customer_id)
                    if order.reference not in claimed
                ]
            )
            for candidate in candidates
        }

        for candidate in candidates:
            features = extract(
                ledger,
                txn,
                candidate,
                candidate_count=len(candidates),
                open_orders_for_customer=open_counts[candidate.customer_id],
                history=history,
            )
            rows.append(
                TrainingRow(
                    transaction_reference=txn.reference,
                    order_references=candidate.order_references,
                    paid_at=txn.paid_at,
                    vector=as_vector(features),
                    label=1 if answer.is_correct(candidate.order_references) else 0,
                )
            )

        # Only learn the payer's habits from payments whose owner we actually
        # know. Recording a guess here would teach the model its own mistakes.
        for reference in answer.order_references[:1]:
            order = ledger.orders.get(reference)
            if order is not None:
                history.record(order.customer_id, observed_name(txn))

    return rows


def split_by_time(
    rows: list[TrainingRow], dev_fraction: float = 0.75
) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Older payments develop the model, the newest ones test it.

    Whole payments stay together: every candidate for one payment lands on the
    same side, so the model is never tested on a payment it has already seen one
    shortlist entry for.

    By time rather than at random, because the payer-history feature remembers
    who has paid for whom. Split randomly and a customer's Tuesday payment
    trains the model that scores their Wednesday one. Every number comes out
    better and means less.
    """
    if not rows:
        return [], []

    payments = sorted({(row.paid_at, row.transaction_reference) for row in rows})
    cut = int(len(payments) * dev_fraction)
    dev_references = {reference for _, reference in payments[:cut]}
    dev = [row for row in rows if row.transaction_reference in dev_references]
    test = [row for row in rows if row.transaction_reference not in dev_references]
    return dev, test


def fold_of(rows: list[TrainingRow], folds: int) -> dict[str, int]:
    """Deal payments into folds, round-robin in time order.

    Round-robin rather than in blocks so every fold covers the whole month. A
    fold that was only the third week would be judged on a week's worth of
    customers.
    """
    payments = sorted({(row.paid_at, row.transaction_reference) for row in rows})
    return {reference: index % folds for index, (_, reference) in enumerate(payments)}


def out_of_fold_scores(rows: list[TrainingRow], folds: int = 4, **fit: Any) -> list[float]:
    """A score for every row, from a model that never saw that row's payment.

    This is what makes the threshold trustworthy on data this size. The residual
    is only about 120 payments; holding a quarter back for calibration left 29
    to choose a threshold on, and 29 payments with zero observed mistakes says
    almost nothing when one mistake costs 83 reviews. Cross-validation spends
    every payment, so the threshold is chosen on all of them instead.
    """
    assignment = fold_of(rows, folds)
    scores = [0.0] * len(rows)
    for fold in range(folds):
        training = [
            (row.vector, row.label) for row in rows if assignment[row.transaction_reference] != fold
        ]
        held_out = [
            index for index, row in enumerate(rows) if assignment[row.transaction_reference] == fold
        ]
        if not held_out:
            continue
        model = LogisticModel(FEATURE_NAMES)
        model.fit(training, **fit)
        for index in held_out:
            scores[index] = model.predict(rows[index].vector)
    return scores


def pick_calibrator(
    rows: list[TrainingRow], scores: list[float], folds: int = 4
) -> tuple[Calibrator, dict[str, float]]:
    """Choose the calibration method without ever looking at the test slice.

    Each fold is calibrated by a calibrator fitted on the other folds, so the
    Brier score being compared is always out-of-sample. Picking the method on
    the test data and then reporting that same test data would be marking your
    own homework.
    """
    assignment = fold_of(rows, folds)
    labels = [row.label for row in rows]

    def fit_and_score(build: Callable[[list[float], list[int]], Calibrator]) -> float:
        predicted: list[float] = []
        actual: list[int] = []
        for fold in range(folds):
            inside = [
                i for i, row in enumerate(rows) if assignment[row.transaction_reference] == fold
            ]
            outside = [
                i for i, row in enumerate(rows) if assignment[row.transaction_reference] != fold
            ]
            if not inside or not outside:
                continue
            calibrator = build([scores[i] for i in outside], [labels[i] for i in outside])
            predicted.extend(calibrator.calibrate(scores[i]) for i in inside)
            actual.extend(labels[i] for i in inside)
        return brier_score(predicted, actual)

    briers = {
        "identity": fit_and_score(lambda _s, _y: IdentityCalibrator()),
        "platt": fit_and_score(lambda s, y: PlattCalibrator.fit(s, y)),
        "isotonic": fit_and_score(lambda s, y: IsotonicCalibrator.fit(s, y)),
    }

    floor = min(briers.values())
    for kind in PREFERENCE:
        if briers[kind] <= floor + TIE_TOLERANCE:
            chosen = kind
            break

    builders: dict[str, Calibrator] = {
        "identity": IdentityCalibrator(),
        "platt": PlattCalibrator.fit(scores, labels),
        "isotonic": IsotonicCalibrator.fit(scores, labels),
    }
    return builders[chosen], briers


def best_per_payment(
    rows: list[TrainingRow], probabilities: list[float]
) -> list[tuple[float, int]]:
    """One (probability, was-it-right) pair per payment, not per candidate.

    This distinction is the difference between a threshold that works and one
    that does not. Each payment has about a dozen candidates and at most one is
    right, so scored row by row the data is 90% negatives and "queue everything"
    looks almost free. But we never decide about a candidate. We decide about a
    payment: clear its best candidate, or hand the payment to a person.

    A payment whose real answer is "this pays no invoice" contributes a row with
    label 0, which is correct: auto-clearing it would be a mistake.
    """
    best: dict[str, tuple[float, int]] = {}
    for row, probability in zip(rows, probabilities, strict=True):
        current = best.get(row.transaction_reference)
        if current is None or probability > current[0]:
            best[row.transaction_reference] = (probability, row.label)
    return [best[reference] for reference in sorted(best)]


def decisions(
    rows: list[TrainingRow], model: LogisticModel, calibrator: Calibrator
) -> list[tuple[float, int]]:
    """`best_per_payment`, scoring the rows with a fitted model first."""
    return best_per_payment(rows, [calibrator.calibrate(model.predict(row.vector)) for row in rows])


def train(
    ledger: Ledger,
    transactions: list[TxnRow],
    truth: dict[str, Truth],
    *,
    costs: CostMatrix | None = None,
    folds: int = 4,
) -> TrainedMatcher:
    """Fit the model, calibrate it, and pick a threshold — in that order, on
    data that was never used for the step before."""
    rows = residual_rows(ledger, transactions, truth)
    dev, test = split_by_time(rows)
    if not dev:
        raise ValueError(
            f"not enough residual to train on: {len(rows)} rows over "
            f"{len({r.transaction_reference for r in rows})} payments"
        )

    # Scores from models that never saw the payment they are scoring.
    oof = out_of_fold_scores(dev, folds=folds)
    calibrator, briers = pick_calibrator(dev, oof, folds=folds)

    # Threshold on every development payment, using those same honest scores.
    per_payment = best_per_payment(dev, [calibrator.calibrate(score) for score in oof])
    threshold = choose_threshold(
        [probability for probability, _ in per_payment],
        [label for _, label in per_payment],
        costs,
    )

    # Only now fit the model that will actually ship, on all of the development
    # data. The test slice has still not been touched.
    model = LogisticModel(FEATURE_NAMES)
    model.fit([(row.vector, row.label) for row in dev])

    return TrainedMatcher(
        model=model,
        calibrator=calibrator,
        threshold=threshold,
        brier_by_method=briers,
        rows_used=len(dev),
        positives=sum(row.label for row in dev),
        dev_payments=len({row.transaction_reference for row in dev}),
        test_payments=len({row.transaction_reference for row in test}),
    )


# ------------------------------------------------------------- saving it


def save(trained: TrainedMatcher, directory: Path) -> None:
    """Write the fitted model somewhere the running service can pick it up.

    Three small JSON files rather than a pickle: they are readable, they diff,
    and nobody can smuggle code into the service by editing one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.json").write_text(json.dumps(trained.model.to_dict(), indent=2) + "\n")
    (directory / "calibrator.json").write_text(
        json.dumps(trained.calibrator.to_dict(), indent=2) + "\n"
    )
    (directory / "threshold.json").write_text(
        json.dumps(
            {
                "threshold": trained.threshold.threshold,
                "chosen_on_payments": trained.dev_payments,
                "cost_matrix": CostMatrix().describe(),
                "expected_cost_a_night_kobo": trained.threshold.expected_cost.kobo,
                "brier_by_calibration_method": trained.brier_by_method,
                "training_rows": trained.rows_used,
                "training_positives": trained.positives,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def load(ledger: Ledger, directory: Path) -> ProbabilisticMatcher:
    """Rebuild the matcher from saved files, no corpus needed."""
    model = LogisticModel.from_dict(json.loads((directory / "model.json").read_text()))
    calibrator = calibration_from_dict(json.loads((directory / "calibrator.json").read_text()))
    settings = json.loads((directory / "threshold.json").read_text())
    return ProbabilisticMatcher(
        ledger=ledger,
        model=model,
        calibrator=calibrator,
        threshold=float(settings["threshold"]),
        history=PayerHistory(),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    from recon.evaluation import truth as truth_module
    from recon.match.ledger import transactions_from_fixtures

    parser = argparse.ArgumentParser(description="Fit the probabilistic matcher.")
    parser.add_argument("--fixtures", type=Path, default=Path("fixtures"))
    parser.add_argument("--out", type=Path, default=Path("models"))
    args = parser.parse_args(argv)

    ledger = Ledger.from_fixtures(args.fixtures)
    transactions = transactions_from_fixtures(args.fixtures)
    answers = truth_module.load(args.fixtures / "ground_truth.json")

    trained = train(ledger, transactions, answers)
    save(trained, args.out)

    print(
        json.dumps(
            {
                "calibration_method": trained.calibrator.to_dict()["kind"],
                "brier_by_method": {k: round(v, 4) for k, v in trained.brier_by_method.items()},
                "threshold": trained.threshold.describe(),
                "top_weights": {
                    name: round(weight, 3)
                    for name, weight in list(trained.model.explain().items())[:8]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
