"""Scoring the reader, on its own, away from the matcher.

Extraction and matching fail for completely different reasons and fixing one
does nothing for the other, so they are measured apart. A combined number would
let a good matcher hide a reader that mangles every third amount.

Two things are scored, and the second one matters more:

* **Field accuracy** — of the reports it read, how many fields did it get right.
  Amount is broken out separately because a wrong amount is a wrong ledger,
  while a missing date is an inconvenience.
* **Knowing when to stop** — of the reports it could not read, how many did it
  correctly refuse. A reader that confidently invents a payer for "someone paid
  20k" scores well on accuracy and is dangerous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from recon.intake.parse import Intake, default_intake
from recon.intake.schema import ParseError
from recon.money import Money

FIELDS = ("amount_kobo", "payer_name", "channel", "order_reference", "paid_on")


@dataclass
class ExtractionReport:
    total: int = 0
    should_parse: int = 0
    should_refuse: int = 0

    parsed_correctly: int = 0
    """Read, and every field right."""

    refused_correctly: int = 0
    """Refused, and refusing was the right call."""

    wrongly_refused: int = 0
    """Could have been read and was not. Costs a person two minutes."""

    wrongly_parsed: int = 0
    """Read something that should have gone to a person. This is the dangerous
    one: it puts an invented payer or amount into the books."""

    field_right: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    mistakes: list[dict[str, Any]] = field(default_factory=list)
    by_extractor: dict[str, int] = field(default_factory=dict)

    @property
    def exact_match_rate(self) -> float:
        """Of the readable reports, how many came out completely right."""
        return self.parsed_correctly / self.should_parse if self.should_parse else 0.0

    @property
    def refusal_rate(self) -> float:
        """Of the unreadable ones, how many were correctly sent to a person."""
        return self.refused_correctly / self.should_refuse if self.should_refuse else 0.0

    @property
    def amount_accuracy(self) -> float:
        total = self.field_total.get("amount_kobo", 0)
        return self.field_right.get("amount_kobo", 0) / total if total else 0.0

    def field_accuracy(self) -> dict[str, float]:
        return {
            name: (self.field_right.get(name, 0) / self.field_total[name])
            for name in FIELDS
            if self.field_total.get(name)
        }


def load_labelled(path: Path) -> list[dict[str, Any]]:
    payload: Any = json.loads(path.read_text())
    return list(payload)


def evaluate(rows: list[dict[str, Any]], intake: Intake | None = None) -> ExtractionReport:
    reader = intake or default_intake()
    report = ExtractionReport(total=len(rows))

    for row in rows:
        expected = row.get("expect")
        today = date.fromisoformat(row.get("today", "2024-05-17"))
        outcome = reader.parse_or_review(row["text"], today)

        if expected is None:
            report.should_refuse += 1
            if isinstance(outcome, ParseError):
                report.refused_correctly += 1
            else:
                report.wrongly_parsed += 1
                report.mistakes.append(
                    {
                        "text": row["text"],
                        "problem": "read something that should have gone to a person",
                        "invented": outcome.report.describe(),
                    }
                )
            continue

        report.should_parse += 1
        if isinstance(outcome, ParseError):
            report.wrongly_refused += 1
            report.mistakes.append(
                {"text": row["text"], "problem": "could not read it", "reason": outcome.reason}
            )
            continue

        report.by_extractor[outcome.extractor] = report.by_extractor.get(outcome.extractor, 0) + 1
        got = outcome.report.model_dump()
        all_right = True
        for name in FIELDS:
            report.field_total[name] = report.field_total.get(name, 0) + 1
            if _same(got.get(name), expected.get(name)):
                report.field_right[name] = report.field_right.get(name, 0) + 1
            else:
                all_right = False
                report.mistakes.append(
                    {
                        "text": row["text"],
                        "problem": f"{name} wrong",
                        "expected": str(expected.get(name)),
                        "got": str(got.get(name)),
                    }
                )
        if all_right:
            report.parsed_correctly += 1

    return report


def _same(got: Any, expected: Any) -> bool:
    if expected is None:
        return got is None
    if isinstance(got, date):
        return got.isoformat() == str(expected)
    return str(got) == str(expected)


def summarise(report: ExtractionReport) -> dict[str, Any]:
    return {
        "reports": report.total,
        "readable": report.should_parse,
        "should_go_to_a_person": report.should_refuse,
        "read_completely_right": f"{report.exact_match_rate:.1%}",
        "correctly_refused": f"{report.refusal_rate:.1%}",
        "amount_accuracy": f"{report.amount_accuracy:.1%}",
        "wrongly_read": report.wrongly_parsed,
        "wrongly_refused": report.wrongly_refused,
        "per_field": {name: f"{value:.1%}" for name, value in report.field_accuracy().items()},
        "read_by": report.by_extractor,
    }


def money_at_stake(rows: list[dict[str, Any]]) -> Money:
    """How much money the labelled set is talking about. Context for the rates."""
    total = 0
    for row in rows:
        expected = row.get("expect")
        if expected:
            total += int(expected.get("amount_kobo", 0))
    return Money(total)
