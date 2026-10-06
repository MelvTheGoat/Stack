"""The workspace the pages read from.

One set of books, one fitted matcher, one set of matches, held in memory and
built on first use. Where the books come from is the one switch:

- **Your own** (the default). Invoices, payments and decisions from the
  database. Anything that changes them calls `changed()`, and the next page
  rebuilds. Rebuilding is the whole matcher over every payment, which is under a
  second for a few hundred and is the honest price of never showing a stale
  answer.
- **The demo** (`RECON_DEMO=1`). The generated corpus in `fixtures/`, loaded
  once. It never changes, so it is never rebuilt.

It is one process. Saying so plainly is better than pretending it is a cluster.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from recon import books
from recon.config import get_settings
from recon.db import session_scope
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
    demo: bool = True

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
#: Bumped by `changed()`. A workspace built for an older number is stale. A
#: counter rather than a flag, so a change that lands while a rebuild is still
#: running is not lost when that rebuild finishes.
_generation = 0
_built_for = -1


def workspace(fixtures: Path = DEFAULT_FIXTURES, models: Path = DEFAULT_MODELS) -> Workspace:
    global _workspace, _built_for
    generation = _generation
    if _workspace is None or (not _workspace.demo and _built_for != generation):
        _workspace = load(fixtures, models) if get_settings().demo else from_database(models)
        _built_for = generation
    return _workspace


def changed() -> None:
    """The books moved. The next page that asks gets them rebuilt."""
    global _generation
    _generation += 1


def from_database(models: Path = DEFAULT_MODELS) -> Workspace:
    """The business's own books.

    The model still runs and still says how sure it is, but it does not close
    anything by itself here. It learned on practice data, and a confidence that
    was checked against made-up customers has not been checked against these.
    """
    with session_scope() as session:
        ledger = books.ledger(session)
        transactions = books.transactions(session)
        decided = books.decisions(session)
        settlements = books.settlements(session)

    probabilistic = None
    if (models / "model.json").exists():
        probabilistic = load_matcher(ledger, models)
        probabilistic.auto_clear = False

    matches = Pipeline.build(ledger, probabilistic).run(transactions, decided)
    return Workspace(
        ledger=ledger,
        transactions=transactions,
        matches=matches,
        settlements=settlements,
        queue=review_queue.build(matches, transactions),
        demo=False,
    )


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
    global _workspace, _built_for
    _workspace = None
    _built_for = -1
