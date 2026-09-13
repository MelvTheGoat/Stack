"""Turning a pile of matches into numbers you can put in a README."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from recon.enums import Layer
from recon.evaluation.truth import Truth
from recon.match.result import Match
from recon.money import Money, sum_money


@dataclass(frozen=True, slots=True)
class LayerScore:
    layer: str
    decided: int
    correct: int
    money_decided: Money

    @property
    def precision(self) -> float:
        """Of the ones this layer answered, how many were right."""
        return self.correct / self.decided if self.decided else 0.0


@dataclass
class Report:
    total: int = 0
    auto_cleared: int = 0
    sent_to_review: int = 0
    correct_auto: int = 0
    by_layer: dict[str, LayerScore] = field(default_factory=dict)
    by_case: dict[str, tuple[int, int]] = field(default_factory=dict)
    money_total: Money = field(default_factory=Money.zero)
    money_auto: Money = field(default_factory=Money.zero)
    money_at_risk: Money = field(default_factory=Money.zero)

    @property
    def auto_clear_rate(self) -> float:
        return self.auto_cleared / self.total if self.total else 0.0

    @property
    def auto_precision(self) -> float:
        """The number that matters: when we close a case unattended, how often
        are we right. A wrong auto-clear is money in the wrong customer's
        account, and nobody finds out until they complain."""
        return self.correct_auto / self.auto_cleared if self.auto_cleared else 0.0

    @property
    def review_rate(self) -> float:
        return self.sent_to_review / self.total if self.total else 0.0


def score(matches: list[Match], truth: dict[str, Truth]) -> Report:
    report = Report(total=len(matches))
    decided: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    money: dict[str, Money] = {}
    case_totals: Counter[str] = Counter()
    case_correct: Counter[str] = Counter()

    for match in matches:
        answer = truth.get(match.transaction_reference)
        if answer is None:
            raise KeyError(
                f"{match.transaction_reference} has no entry in the answer key; "
                "the corpus and the matches are out of step"
            )

        right = answer.is_correct(match.order_references)
        report.money_total = report.money_total + match.money_at_risk
        case_totals[answer.case] += 1

        if match.resolved:
            report.auto_cleared += 1
            report.money_auto = report.money_auto + match.money_at_risk
            layer = str(match.layer)
            decided[layer] += 1
            money[layer] = money.get(layer, Money.zero()) + match.money_at_risk
            if right:
                report.correct_auto += 1
                correct[layer] += 1
                case_correct[answer.case] += 1
        else:
            report.sent_to_review += 1
            report.money_at_risk = report.money_at_risk + match.money_at_risk
            # A case sent to a human is not scored as wrong; it is scored as
            # work. Whether that work was worth doing is the threshold's
            # problem, not the matcher's.

    report.by_layer = {
        layer: LayerScore(layer, decided[layer], correct[layer], money.get(layer, Money.zero()))
        for layer in sorted(decided)
    }
    report.by_case = {case: (case_correct[case], case_totals[case]) for case in sorted(case_totals)}
    return report


def unresolved_money(matches: list[Match]) -> Money:
    return sum_money([m.money_at_risk for m in matches if m.layer is Layer.UNRESOLVED])
