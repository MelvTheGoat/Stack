"""Making the number mean what it says.

A logistic model outputs something between 0 and 1, and it is tempting to call
that a probability. It usually is not. If you take every case the model scored
0.8 and check how many were actually right, you might find 55%. The model is
not lying, it was never asked to be right about its own confidence — it was
asked to rank.

That gap matters here, because the whole design rests on "auto-clear anything
above the threshold". If 0.9 really means 62%, then a threshold of 0.9 is
quietly clearing four wrong matches in every ten, and nobody finds out until a
customer rings up about an invoice they already paid.

So: fit the model, then fit a second, tiny thing that maps its scores onto
honest probabilities, and check the result with a reliability diagram and a
Brier score.

Two methods, and we pick between them on held-out data rather than on taste:

* **Platt scaling** — fit a one-dimensional logistic on the scores. Two
  parameters, so it cannot overfit, but it can only stretch and shift the curve.
* **Isotonic regression** — fit any non-decreasing step function. Far more
  flexible, and on a few hundred points it will happily memorise noise.

Neither is right in general. The harness fits both and keeps whichever scores a
better Brier on data it has not seen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import exp, log
from pathlib import Path
from typing import Any, Protocol


class Calibrator(Protocol):
    def calibrate(self, score: float) -> float: ...
    def to_dict(self) -> dict[str, Any]: ...


@dataclass
class IdentityCalibrator:
    """Leave the score alone. The baseline both methods have to beat."""

    name: str = "identity"

    def calibrate(self, score: float) -> float:
        return min(max(score, 0.0), 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "identity"}


@dataclass
class PlattCalibrator:
    """p = sigmoid(a * score + b). Two knobs, hard to overfit."""

    a: float = 1.0
    b: float = 0.0
    name: str = "platt"

    def calibrate(self, score: float) -> float:
        z = self.a * score + self.b
        if z >= 0:
            return 1.0 / (1.0 + exp(-z))
        positive = exp(z)
        return positive / (1.0 + positive)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "platt", "a": self.a, "b": self.b}

    @classmethod
    def fit(
        cls, scores: list[float], labels: list[int], *, epochs: int = 2000, lr: float = 0.5
    ) -> PlattCalibrator:
        model = cls()
        n = len(scores) or 1
        for _ in range(epochs):
            grad_a = grad_b = 0.0
            for score, label in zip(scores, labels, strict=True):
                error = model.calibrate(score) - label
                grad_a += error * score
                grad_b += error
            model.a -= lr * grad_a / n
            model.b -= lr * grad_b / n
        return model


@dataclass
class IsotonicCalibrator:
    """A non-decreasing step function fitted by pool-adjacent-violators.

    Flexible enough to fix a badly shaped curve, and flexible enough to memorise
    a small sample, which is why it only gets used if it wins on held-out data.
    """

    thresholds: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    name: str = "isotonic"

    def calibrate(self, score: float) -> float:
        if not self.thresholds:
            return min(max(score, 0.0), 1.0)
        # Step function: take the value of the last block starting at or below
        # this score.
        chosen = self.values[0]
        for threshold, value in zip(self.thresholds, self.values, strict=True):
            if score >= threshold:
                chosen = value
            else:
                break
        return min(max(chosen, 0.0), 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "isotonic", "thresholds": self.thresholds, "values": self.values}

    @classmethod
    def fit(cls, scores: list[float], labels: list[int]) -> IsotonicCalibrator:
        """Pool adjacent violators.

        Sort by score, then repeatedly merge any neighbouring pair whose fitted
        values go downhill, averaging them. What is left is the closest
        non-decreasing fit to the labels.
        """
        if not scores:
            return cls()

        # Pool identical scores first. Without this, sorting (score, label)
        # puts every 0 before every 1 inside a tied group, which already looks
        # non-decreasing, so PAVA leaves it alone and the last block at that
        # score reads 100%. Two hundred cases scored 0.9 of which half were
        # right would come back as "0.9 means certain", which is the exact
        # error calibration exists to catch.
        pooled: dict[float, list[float]] = {}
        for score, label in zip(scores, labels, strict=True):
            bucket = pooled.setdefault(score, [0.0, 0.0])
            bucket[0] += label
            bucket[1] += 1

        blocks: list[list[float]] = []  # [start_score, sum_of_labels, count]
        for score in sorted(pooled):
            total, count = pooled[score]
            blocks.append([score, total, count])
            while len(blocks) > 1:
                previous, current = blocks[-2], blocks[-1]
                if previous[1] / previous[2] <= current[1] / current[2]:
                    break
                previous[1] += current[1]
                previous[2] += current[2]
                blocks.pop()

        return cls(
            thresholds=[block[0] for block in blocks],
            values=[block[1] / block[2] for block in blocks],
        )


def from_dict(payload: dict[str, Any]) -> Calibrator:
    kind = payload.get("kind", "identity")
    if kind == "platt":
        return PlattCalibrator(a=float(payload["a"]), b=float(payload["b"]))
    if kind == "isotonic":
        return IsotonicCalibrator(
            thresholds=list(payload["thresholds"]), values=list(payload["values"])
        )
    return IdentityCalibrator()


# ----------------------------------------------------------- measuring it


def brier_score(probabilities: list[float], labels: list[int]) -> float:
    """Mean squared error of the probabilities themselves.

    0 is perfect. 0.25 is what you get by saying "50%" to everything. It
    punishes being confidently wrong much harder than being unsure, which is
    exactly the trade-off we care about.
    """
    if not probabilities:
        return 0.0
    total = sum((p - label) ** 2 for p, label in zip(probabilities, labels, strict=True))
    return total / len(probabilities)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    actual_rate: float

    @property
    def gap(self) -> float:
        """How far the claim is from the truth, in this bin."""
        return self.actual_rate - self.mean_predicted


def reliability(
    probabilities: list[float], labels: list[int], bins: int = 10
) -> list[ReliabilityBin]:
    """The reliability diagram, as numbers.

    Read it like this: of the cases where we said about 80%, how many were
    actually right? If the answer is 80%, the column sits on the diagonal and
    the number means what it says.
    """
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, label in zip(probabilities, labels, strict=True):
        index = min(int(probability * bins), bins - 1)
        buckets[index].append((probability, label))

    out: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        lower, upper = index / bins, (index + 1) / bins
        if not bucket:
            out.append(ReliabilityBin(lower, upper, 0, 0.0, 0.0))
            continue
        mean_predicted = sum(p for p, _ in bucket) / len(bucket)
        actual = sum(label for _, label in bucket) / len(bucket)
        out.append(ReliabilityBin(lower, upper, len(bucket), mean_predicted, actual))
    return out


def expected_calibration_error(bins: list[ReliabilityBin]) -> float:
    """One number for "how far off are the claims", weighted by how many cases."""
    total = sum(b.count for b in bins)
    if not total:
        return 0.0
    return sum(b.count * abs(b.gap) for b in bins) / total


def log_loss(probabilities: list[float], labels: list[int]) -> float:
    if not probabilities:
        return 0.0
    total = 0.0
    for probability, label in zip(probabilities, labels, strict=True):
        clipped = min(max(probability, 1e-12), 1 - 1e-12)
        total += -(label * log(clipped) + (1 - label) * log(1 - clipped))
    return total / len(probabilities)


#: Preference order when two methods score about the same. Smoother first.
#: Isotonic only wins if it is clearly better, for the reason in `choose`.
PREFERENCE: tuple[str, ...] = ("identity", "platt", "isotonic")

#: How much better isotonic has to be, in Brier, before we take it anyway.
TIE_TOLERANCE = 0.01


def choose(
    scores: list[float], labels: list[int], holdout_scores: list[float], holdout_labels: list[int]
) -> tuple[Calibrator, dict[str, float]]:
    """Fit every method, score them all on held-out data, return the winner.

    Not simply the lowest Brier. On this data isotonic scored 0.0316 against
    Platt's 0.0326 — a rounding error apart — and it did it by collapsing every
    probability onto six distinct values. That is fine for a Brier score and
    useless for a threshold: every line between 0.6 and 0.99 cleared exactly the
    same cases, so "where do we draw the line" stopped having an answer.

    So a method only displaces a smoother one if it is better by a real margin.
    Being 0.001 better at a scoring rule is not worth losing the ability to set
    a threshold.

    Returns the chosen calibrator and every method's Brier score, including the
    ones that lost. A calibration step that only reports its own winner is not
    evidence of anything.
    """
    candidates: list[Calibrator] = [
        IdentityCalibrator(),
        PlattCalibrator.fit(scores, labels),
        IsotonicCalibrator.fit(scores, labels),
    ]

    briers: dict[str, float] = {}
    scored: list[tuple[float, int, Calibrator]] = []
    for calibrator in candidates:
        calibrated = [calibrator.calibrate(s) for s in holdout_scores]
        brier = brier_score(calibrated, holdout_labels)
        kind = str(calibrator.to_dict()["kind"])
        briers[kind] = brier
        scored.append((brier, PREFERENCE.index(kind), calibrator))

    floor = min(brier for brier, _, _ in scored)
    within_reach = [entry for entry in scored if entry[0] <= floor + TIE_TOLERANCE]
    within_reach.sort(key=lambda entry: entry[1])
    return within_reach[0][2], briers


def save(calibrator: Calibrator, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibrator.to_dict(), indent=2) + "\n")


def load(path: Path) -> Calibrator:
    return from_dict(json.loads(path.read_text()))
