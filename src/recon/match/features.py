"""Turning a (payment, candidate invoice) pair into numbers.

Every feature here is something a bookkeeper would actually look at, and each
one is named after the question it answers. That matters twice over: the model's
weights are readable afterwards, and the review queue can show a person the same
evidence the model used, in words.

All features are scaled to roughly 0..1 so the trained weights are comparable
to each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import exp

from recon.match.candidates import Candidate, observed_name
from recon.match.ledger import Ledger, TxnRow
from recon.match.similarity import name_similarity, surname_matches
from recon.money import Money

FEATURE_NAMES: tuple[str, ...] = (
    "name_similarity",
    "surname_exact",
    "landed_in_their_account",
    "amount_exact",
    "amount_closeness",
    "underpaid",
    "overpaid",
    "covers_several_invoices",
    "recency",
    "inside_the_usual_window",
    "paid_by_card",
    "paid_by_dedicated_account",
    "paid_by_transfer",
    "paid_in_cash",
    "their_only_open_invoice",
    "few_other_candidates",
    "we_have_seen_this_payer_before",
    "round_number_payment",
)


@dataclass
class PayerHistory:
    """Who has paid for whom before, and under what name.

    Regulars are the easy half of the residual: Ada always transfers as
    "OKONKWO ADA" from the same bank, and once that has happened twice it is
    worth more than any single spelling comparison.
    """

    names_by_customer: dict[str, list[str]] = field(default_factory=dict)

    def record(self, customer_id: str, observed: str) -> None:
        if observed.strip():
            self.names_by_customer.setdefault(customer_id, []).append(observed)

    def familiarity(self, customer_id: str, observed: str) -> float:
        """Best similarity between this name and names that paid for them before."""
        seen = self.names_by_customer.get(customer_id, ())
        if not seen or not observed.strip():
            return 0.0
        return max(name_similarity(observed, past) for past in seen[-10:])


def extract(
    ledger: Ledger,
    txn: TxnRow,
    candidate: Candidate,
    *,
    candidate_count: int,
    open_orders_for_customer: int,
    history: PayerHistory | None = None,
) -> dict[str, float]:
    """The feature row for one payment against one candidate."""
    customer = ledger.customers.get(candidate.customer_id)
    text = observed_name(txn)
    paid, owed = txn.amount, candidate.total

    first_order = min(
        (ledger.orders[r].issued_at for r in candidate.order_references),
        default=txn.paid_at,
    )
    gap_days = max(0.0, (txn.paid_at - first_order) / timedelta(days=1))

    return {
        "name_similarity": name_similarity(text, customer.name) if customer else 0.0,
        "surname_exact": 1.0 if customer and surname_matches(text, customer.name) else 0.0,
        "landed_in_their_account": 1.0 if candidate.via == "dva" else 0.0,
        "amount_exact": 1.0 if paid == owed else 0.0,
        "amount_closeness": _closeness(paid, owed),
        "underpaid": 1.0 if paid < owed else 0.0,
        "overpaid": 1.0 if paid > owed else 0.0,
        "covers_several_invoices": 1.0 if candidate.is_combination else 0.0,
        # Decays with a one-week half-life: a payment four days after the
        # invoice is ordinary, forty days after is a different conversation.
        "recency": exp(-gap_days / 7.0),
        "inside_the_usual_window": 1.0 if gap_days <= 10 else 0.0,
        "paid_by_card": 1.0 if txn.channel == "card" else 0.0,
        "paid_by_dedicated_account": 1.0 if txn.channel == "dva" else 0.0,
        "paid_by_transfer": 1.0 if txn.channel == "bank_transfer" else 0.0,
        "paid_in_cash": 1.0 if txn.channel == "cash" else 0.0,
        "their_only_open_invoice": 1.0 if open_orders_for_customer == 1 else 0.0,
        # One candidate is a much better position than twelve, and the model
        # should be allowed to know that.
        "few_other_candidates": 1.0 / (1.0 + max(0, candidate_count - 1)),
        "we_have_seen_this_payer_before": (
            history.familiarity(candidate.customer_id, text) if history else 0.0
        ),
        "round_number_payment": 1.0 if _is_round(paid) else 0.0,
    }


def as_vector(features: dict[str, float]) -> list[float]:
    return [features[name] for name in FEATURE_NAMES]


def _closeness(paid: Money, owed: Money) -> float:
    """1.0 when the amounts are equal, falling away in both directions."""
    if owed.kobo <= 0 or paid.kobo <= 0:
        return 0.0
    smaller, larger = sorted((paid.kobo, owed.kobo))
    return smaller / larger


def _is_round(amount: Money) -> bool:
    """Whole thousands of naira. People round up when they are paying in a hurry."""
    return amount.kobo % 100_000 == 0
