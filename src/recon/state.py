"""The workspace the pages read from.

One loaded corpus, one fitted matcher, one set of matches, held in memory and
built on first use. It is a single-process demo, and saying so plainly is better
than pretending it is a cluster.

A real deployment replaces this with reads from Postgres: the templates and the
report take plain data, so nothing else has to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from recon.match.ledger import Ledger, TxnRow, transactions_from_fixtures
from recon.match.pipeline import Pipeline
from recon.match.result import Match
from recon.match.training import load as load_matcher
from recon.review import queue as review_queue

DEFAULT_FIXTURES = Path("fixtures")
DEFAULT_MODELS = Path("models")


@dataclass
class Workspace:
    ledger: Ledger
    transactions: list[TxnRow]
    matches: list[Match]
    settlements: list[dict[str, Any]]
    queue: review_queue.Queue
    reviewer: str = "melvyn"
    decided: set[str] = field(default_factory=set)

    def item(self, reference: str) -> review_queue.QueueItem | None:
        for entry in self.queue.items:
            if entry.transaction_reference == reference:
                return entry
        return None

    def mark_decided(self, reference: str) -> None:
        self.decided.add(reference)

    def last_trading_day(self) -> date:
        """The most recent day anything was taken. What the report opens on."""
        if not self.transactions:
            return date.today()
        return max(txn.paid_at.date() for txn in self.transactions)


_workspace: Workspace | None = None


def workspace(fixtures: Path = DEFAULT_FIXTURES, models: Path = DEFAULT_MODELS) -> Workspace:
    global _workspace
    if _workspace is None:
        _workspace = load(fixtures, models)
    return _workspace


def load(fixtures: Path = DEFAULT_FIXTURES, models: Path = DEFAULT_MODELS) -> Workspace:
    ledger = Ledger.from_fixtures(fixtures)
    transactions = transactions_from_fixtures(fixtures)

    probabilistic = None
    if (models / "model.json").exists():
        probabilistic = load_matcher(ledger, models)

    matches = Pipeline.build(ledger, probabilistic).run(transactions)
    settlements_path = fixtures / "settlements.json"
    settlements = (
        list(json.loads(settlements_path.read_text())) if settlements_path.exists() else []
    )

    return Workspace(
        ledger=ledger,
        transactions=transactions,
        matches=matches,
        settlements=settlements,
        queue=review_queue.build(matches, transactions),
    )


def reset() -> None:
    """Forget the loaded workspace. Tests call this between runs."""
    global _workspace
    _workspace = None
