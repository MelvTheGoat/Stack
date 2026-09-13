"""Reading the answer key, and deciding whether an answer is right.

Scoring sounds trivial until you meet the twin invoices, where two answers are
equally correct, and the duplicates, where the correct answer is "this pays
nothing". Both live here so every report scores them the same way.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Truth:
    transaction_reference: str
    order_references: tuple[str, ...]
    case: str
    winnable_by: str
    name_mangling: str = ""
    alternatives: bool = False
    duplicate_of: str | None = None
    note: str = ""

    def is_correct(self, answer: Sequence[str]) -> bool:
        """Did the matcher get this one right?

        Three shapes of correct:

        * ordinary — the set of invoices must be exactly right
        * alternatives — two invoices fit equally well, so naming either one is
          right, and naming both is not (that would be claiming the payment
          settled twice as much money as it did)
        * pays nothing — a duplicate or a stray payment, where the only right
          answer is to name no invoice at all
        """
        got = sorted(set(answer))
        want = sorted(set(self.order_references))
        if self.alternatives:
            return len(got) == 1 and got[0] in want
        return got == want

    @property
    def pays_nothing(self) -> bool:
        return not self.order_references


def load(path: Path) -> dict[str, Truth]:
    rows: Any = json.loads(path.read_text())
    return {row["transaction_reference"]: _row(row) for row in rows}


def _row(row: dict[str, Any]) -> Truth:
    return Truth(
        transaction_reference=row["transaction_reference"],
        order_references=tuple(row.get("order_references", ())),
        case=row["case"],
        winnable_by=row.get("winnable_by", ""),
        name_mangling=row.get("name_mangling", ""),
        alternatives=bool(row.get("alternatives", False)),
        duplicate_of=row.get("duplicate_of"),
        note=row.get("note", ""),
    )


def from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Truth]:
    return {row["transaction_reference"]: _row(row) for row in rows}
