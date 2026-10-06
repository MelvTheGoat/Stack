"""Running the layers in order, which is the whole architecture in one file.

Deterministic first, probabilistic second, generative last. Nothing reaches a
layer that an earlier layer could have settled, and every decision records which
layer made it, so the mix can be reported for any given day rather than guessed
at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recon.enums import DETERMINISTIC_LAYERS, Layer
from recon.match.deterministic import DeterministicMatcher
from recon.match.ledger import Ledger, TxnRow
from recon.match.probabilistic import ProbabilisticMatcher
from recon.match.result import Match


@dataclass
class Pipeline:
    ledger: Ledger
    deterministic: DeterministicMatcher
    probabilistic: ProbabilisticMatcher | None = None

    _claimed: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def build(cls, ledger: Ledger, probabilistic: ProbabilisticMatcher | None = None) -> Pipeline:
        return cls(
            ledger=ledger,
            deterministic=DeterministicMatcher(ledger),
            probabilistic=probabilistic,
        )

    def run(self, transactions: list[TxnRow]) -> list[Match]:
        """Both layers, oldest payment first.

        Order matters: an invoice settled by an earlier payment is off the table
        for a later one, and that is often what breaks a tie further down the
        evening.
        """
        ordered = sorted(transactions, key=lambda t: (t.paid_at, t.reference))
        by_reference = {t.reference: t for t in ordered}

        first_pass = self.deterministic.match_all(ordered)
        self._claimed = {reference for match in first_pass for reference in match.order_references}

        if self.probabilistic is None:
            return first_pass

        final: list[Match] = []
        for match in first_pass:
            if match.layer is not Layer.UNRESOLVED:
                final.append(match)
                continue
            second = self.probabilistic.match(
                by_reference[match.transaction_reference], self._claimed
            )
            self._claimed.update(second.order_references)
            final.append(second)
        return final


def layer_mix(matches: list[Match]) -> dict[str, float]:
    """What fraction of the night each layer resolved.

    This is the number the architecture lives or dies by. If the probabilistic
    layer is doing work the deterministic layer could have done, that shows up
    here as the mix drifting, and it means a rule is missing rather than that
    the model is clever.
    """
    total = len(matches) or 1
    counts: dict[str, int] = {"deterministic": 0, "probabilistic": 0, "human": 0}
    for match in matches:
        if match.needs_human or match.layer is Layer.UNRESOLVED:
            counts["human"] += 1
        elif match.layer in DETERMINISTIC_LAYERS:
            counts["deterministic"] += 1
        else:
            counts["probabilistic"] += 1
    return {name: count / total for name, count in counts.items()}
