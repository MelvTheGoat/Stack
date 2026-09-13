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

from dataclasses import dataclass, field
from datetime import datetime

from recon.evaluation.truth import Truth
from recon.match.calibration import Calibrator, choose
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


def decisions(
    rows: list[TrainingRow], model: LogisticModel, calibrator: Calibrator
) -> list[tuple[float, int]]:
    """One (probability, was-it-right) pair per payment, not per candidate.

    This distinction is the difference between a threshold that works and one
    that does not. Each payment has about a dozen candidates and at most one is
    right, so scored row by row the data is 90% negatives and "queue everything"
    looks almost free. But we never decide about a candidate. We decide about a
    payment: clear its best candidate, or hand the payment to a person. So the
    cost sweep has to see one row per payment, carrying the best candidate's
    probability and whether that candidate was the right one.

    A payment whose real answer is "this pays no invoice" contributes a row with
    label 0, which is correct: auto-clearing it would be a mistake.
    """
    best: dict[str, tuple[float, int]] = {}
    for row in rows:
        probability = calibrator.calibrate(model.predict(row.vector))
        current = best.get(row.transaction_reference)
        if current is None or probability > current[0]:
            best[row.transaction_reference] = (probability, row.label)
    return [best[reference] for reference in sorted(best)]


@dataclass
class TrainedMatcher:
    model: LogisticModel
    calibrator: Calibrator
    threshold: ThresholdChoice
    brier_by_method: dict[str, float] = field(default_factory=dict)
    rows_used: int = 0
    positives: int = 0

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
    rows: list[TrainingRow], train_fraction: float = 0.5, validate_fraction: float = 0.25
) -> tuple[list[TrainingRow], list[TrainingRow], list[TrainingRow]]:
    """Oldest rows train, middle rows calibrate, newest rows test.

    Whole payments stay together: every candidate for one payment lands in the
    same slice, so the model is never tested on a payment it has already seen
    one shortlist entry for.
    """
    if not rows:
        return [], [], []

    references = sorted({(row.paid_at, row.transaction_reference) for row in rows})
    train_end = int(len(references) * train_fraction)
    validate_end = int(len(references) * (train_fraction + validate_fraction))

    slice_of = {
        reference: ("train" if i < train_end else "validate" if i < validate_end else "test")
        for i, (_, reference) in enumerate(references)
    }
    buckets: dict[str, list[TrainingRow]] = {"train": [], "validate": [], "test": []}
    for row in rows:
        buckets[slice_of[row.transaction_reference]].append(row)
    return buckets["train"], buckets["validate"], buckets["test"]


def train(
    ledger: Ledger,
    transactions: list[TxnRow],
    truth: dict[str, Truth],
    *,
    costs: CostMatrix | None = None,
) -> TrainedMatcher:
    rows = residual_rows(ledger, transactions, truth)
    train_rows, validate_rows, test_rows = split_by_time(rows)
    if not train_rows or not validate_rows:
        raise ValueError(
            f"not enough residual to train on: {len(rows)} rows over "
            f"{len({r.transaction_reference for r in rows})} payments"
        )

    model = LogisticModel(FEATURE_NAMES)
    model.fit([(row.vector, row.label) for row in train_rows])

    validate_scores = [model.predict(row.vector) for row in validate_rows]
    validate_labels = [row.label for row in validate_rows]
    holdout_rows = test_rows or validate_rows
    holdout_scores = [model.predict(row.vector) for row in holdout_rows]
    holdout_labels = [row.label for row in holdout_rows]

    calibrator, briers = choose(validate_scores, validate_labels, holdout_scores, holdout_labels)

    # The threshold is picked on the calibration slice, never on the test slice.
    # Choosing it on test would be choosing the answer and then reporting it.
    per_payment = decisions(validate_rows, model, calibrator)
    threshold = choose_threshold(
        [probability for probability, _ in per_payment],
        [label for _, label in per_payment],
        costs,
    )

    return TrainedMatcher(
        model=model,
        calibrator=calibrator,
        threshold=threshold,
        brier_by_method=briers,
        rows_used=len(train_rows),
        positives=sum(row.label for row in train_rows),
    )
