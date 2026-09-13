"""A small logistic regression, written out longhand.

No scikit-learn. Not because it is bad — it is excellent — but because this is
eighteen features and a few thousand rows, and having the fit, the calibration
and the scoring all readable in one file is worth more here than the speed. It
also means the repo installs with no compiler and the numbers are reproducible
anywhere.

The model answers one question: given this payment and this candidate invoice,
what is the chance they belong together. The raw output of a logistic fit is
*not* that chance, which is why calibration lives next door.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from math import exp, log
from pathlib import Path
from typing import Any

Row = tuple[list[float], int]


def sigmoid(z: float) -> float:
    """Squash a score onto 0..1, without overflowing on a confident one."""
    if z >= 0:
        return 1.0 / (1.0 + exp(-z))
    positive = exp(z)
    return positive / (1.0 + positive)


@dataclass
class LogisticModel:
    feature_names: tuple[str, ...]
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0

    def __post_init__(self) -> None:
        if not self.weights:
            self.weights = [0.0] * len(self.feature_names)

    def raw_score(self, vector: list[float]) -> float:
        total = self.bias
        for weight, value in zip(self.weights, vector, strict=True):
            total += weight * value
        return total

    def predict(self, vector: list[float]) -> float:
        return sigmoid(self.raw_score(vector))

    def fit(
        self,
        rows: list[Row],
        *,
        epochs: int = 300,
        learning_rate: float = 0.3,
        l2: float = 0.001,
        seed: int = 17,
    ) -> LogisticModel:
        """Plain gradient descent with L2 and class balancing.

        Class balancing matters more than anything else here. A payment has one
        right invoice and a dozen wrong ones, so about 92% of training rows are
        negative. Left alone the model learns to say "no" to everything, scores
        92% accuracy, and is useless. Weighting each class by its scarcity fixes
        that, and it is one line.
        """
        if not rows:
            raise ValueError("cannot fit a model on no data")

        rng = random.Random(seed)
        order = list(range(len(rows)))
        positives = sum(1 for _, label in rows if label == 1)
        negatives = len(rows) - positives
        if positives == 0 or negatives == 0:
            raise ValueError(
                f"training data has only one class ({positives} positive, {negatives} negative); "
                "a model fitted on it would be a constant"
            )
        weight_for = {
            1: len(rows) / (2.0 * positives),
            0: len(rows) / (2.0 * negatives),
        }

        for _ in range(epochs):
            rng.shuffle(order)
            gradient = [0.0] * len(self.weights)
            bias_gradient = 0.0
            for index in order:
                vector, label = rows[index]
                error = (self.predict(vector) - label) * weight_for[label]
                for position, value in enumerate(vector):
                    gradient[position] += error * value
                bias_gradient += error

            scale = learning_rate / len(rows)
            for position in range(len(self.weights)):
                self.weights[position] -= scale * gradient[position] + l2 * self.weights[position]
            self.bias -= scale * bias_gradient

        return self

    # ------------------------------------------------------------ readable

    def explain(self) -> dict[str, float]:
        """The learned weights, biggest first. Worth actually reading."""
        pairs = dict(zip(self.feature_names, self.weights, strict=True))
        return dict(sorted(pairs.items(), key=lambda kv: -abs(kv[1])))

    # -------------------------------------------------------------- saving

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "weights": self.weights,
            "bias": self.bias,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LogisticModel:
        return cls(
            feature_names=tuple(payload["feature_names"]),
            weights=list(payload["weights"]),
            bias=float(payload["bias"]),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> LogisticModel:
        return cls.from_dict(json.loads(path.read_text()))


def log_loss(predictions: list[float], labels: list[int]) -> float:
    """Average surprise. Lower is better; used to watch the fit converge."""
    total = 0.0
    for probability, label in zip(predictions, labels, strict=True):
        clipped = min(max(probability, 1e-12), 1 - 1e-12)
        total += -(label * log(clipped) + (1 - label) * log(1 - clipped))
    return total / len(predictions) if predictions else 0.0
