"""Layer two: scoring what layer one could not settle.

Only the residual reaches here. For each leftover payment we build a shortlist
of invoices it could be for, score each one, turn the score into an honest
probability, and then either close the case or hand it to a person depending on
which side of the threshold it falls.

What gets handed over is not just "we are unsure". Every queued case carries its
top few candidates, the probability of each, and the features that drove the
score, in words, so the person deciding is reading evidence rather than being
asked to trust a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from recon.enums import Layer
from recon.match.calibration import Calibrator, IdentityCalibrator
from recon.match.candidates import Candidate, observed_name, shortlist
from recon.match.features import PayerHistory, as_vector, extract
from recon.match.ledger import Ledger, TxnRow
from recon.match.model import LogisticModel
from recon.match.result import Match, unresolved

#: How many candidates to show a reviewer. More than three is a wall of text.
EVIDENCE_DEPTH = 3


@dataclass(frozen=True, slots=True)
class Scored:
    candidate: Candidate
    probability: float
    features: dict[str, float]


@dataclass
class ProbabilisticMatcher:
    ledger: Ledger
    model: LogisticModel
    calibrator: Calibrator = field(default_factory=IdentityCalibrator)
    threshold: float = 0.9
    history: PayerHistory = field(default_factory=PayerHistory)

    def score_candidates(self, txn: TxnRow, claimed: set[str]) -> list[Scored]:
        candidates = shortlist(self.ledger, txn, claimed)
        if not candidates:
            return []

        open_counts = {
            candidate.customer_id: len(
                [
                    order
                    for order in self.ledger.orders_for_customer(candidate.customer_id)
                    if order.reference not in claimed
                ]
            )
            for candidate in candidates
        }

        scored: list[Scored] = []
        for candidate in candidates:
            features = extract(
                self.ledger,
                txn,
                candidate,
                candidate_count=len(candidates),
                open_orders_for_customer=open_counts[candidate.customer_id],
                history=self.history,
            )
            raw = self.model.predict(as_vector(features))
            scored.append(Scored(candidate, self.calibrator.calibrate(raw), features))

        scored.sort(key=lambda s: (-s.probability, s.candidate.order_references))
        return scored

    def match(self, txn: TxnRow, claimed: set[str]) -> Match:
        scored = self.score_candidates(txn, claimed)

        if not scored:
            return unresolved(
                txn.reference,
                "no invoice it could plausibly be",
                txn.amount,
                channel=txn.channel,
                narration=txn.narration,
                payer_name=txn.payer_name,
            )

        best = scored[0]
        evidence = _evidence(self.ledger, txn, scored)

        if best.probability >= self.threshold:
            self.history.record(best.candidate.customer_id, observed_name(txn))
            return Match(
                transaction_reference=txn.reference,
                order_references=best.candidate.order_references,
                layer=Layer.PROBABILISTIC,
                confidence=best.probability,
                evidence={"rule": "probabilistic", **evidence},
                money_at_risk=txn.amount,
            )

        return Match(
            transaction_reference=txn.reference,
            order_references=(),
            layer=Layer.UNRESOLVED,
            confidence=best.probability,
            evidence={
                "rule": "unresolved",
                "reason": (
                    f"best guess is {best.probability:.0%}, below the {self.threshold:.0%} line"
                ),
                **evidence,
            },
            needs_human=True,
            money_at_risk=txn.amount,
        )


def _evidence(ledger: Ledger, txn: TxnRow, scored: list[Scored]) -> dict[str, Any]:
    """What a person needs to see to check the answer in ten seconds."""
    return {
        "paid": str(txn.amount),
        "channel": txn.channel,
        "narration": txn.narration,
        "payer_name": txn.payer_name,
        "candidates": [
            {
                "orders": list(item.candidate.order_references),
                "customer": _customer_name(ledger, item.candidate.customer_id),
                "invoice_total": str(item.candidate.total),
                "confidence": round(item.probability, 4),
                "found_because": item.candidate.via,
                "why": _in_words(item.features),
            }
            for item in scored[:EVIDENCE_DEPTH]
        ],
        "runner_up_gap": (
            round(scored[0].probability - scored[1].probability, 4) if len(scored) > 1 else None
        ),
    }


def _customer_name(ledger: Ledger, customer_id: str) -> str:
    customer = ledger.customers.get(customer_id)
    return customer.name if customer else customer_id


def _in_words(features: dict[str, float]) -> list[str]:
    """Say what the model noticed, in a sentence a bookkeeper can check."""
    reasons: list[str] = []
    name = features.get("name_similarity", 0.0)
    if name >= 0.9:
        reasons.append("the name matches")
    elif name >= 0.6:
        reasons.append(f"the name is close ({name:.0%})")
    else:
        reasons.append(f"the name barely matches ({name:.0%})")

    if features.get("landed_in_their_account"):
        reasons.append("it landed in their own account number")
    if features.get("amount_exact"):
        reasons.append("the amount is exact")
    elif features.get("underpaid"):
        reasons.append(f"it is short ({features.get('amount_closeness', 0):.0%} of the invoice)")
    elif features.get("overpaid"):
        reasons.append("it is more than the invoice")
    if features.get("covers_several_invoices"):
        reasons.append("it adds up to several invoices exactly")
    if features.get("their_only_open_invoice"):
        reasons.append("it is their only invoice still open")
    if features.get("we_have_seen_this_payer_before", 0.0) >= 0.8:
        reasons.append("this payer has paid for them before under the same name")
    if not features.get("inside_the_usual_window"):
        reasons.append("but the invoice is older than usual")
    return reasons
