"""Layer one: the things we can know for certain.

The house rule is deterministic first, probabilistic second, generative last,
and this is the first layer. Everything in here is an exact lookup. Nothing
here scores, guesses or approximates, and nothing here fires when there is more
than one candidate — if two invoices fit, that is not certainty and the case
goes down to the next layer.

Four rules, cheapest first:

1. **Already seen.** Same money, same payer, same day, and the invoice it points
   at is already settled. That is a double submission, and it pays nothing new.
2. **Exact reference.** The order reference came through as structured data, or
   it is sitting in the narration and it matches a real invoice exactly.
3. **Dedicated account.** Money landed in an account number that belongs to one
   customer, and that customer has exactly one open invoice it could be.
4. **Amount and time window.** Exactly one open invoice in the last few days is
   for precisely this amount, to the kobo.

Whatever is left over is the residual, and it is the probabilistic layer's
problem. The share this layer closes is the baseline every later number is
measured against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from recon.corpus import names
from recon.enums import Layer
from recon.match.ledger import Ledger, OrderRow, TxnRow
from recon.match.result import Match, unresolved

#: Order references look like INV-0042. Anything matching this shape is *checked
#: against the real ledger* before it is believed, so a false positive would
#: need to be a string that is character-for-character a live invoice number.
REFERENCE_PATTERN = re.compile(r"\b([A-Z]{2,6}[-/ ]?\d{3,8})\b", re.IGNORECASE)

#: How long after an invoice is issued a payment can still be matched to it on
#: amount alone. Ten days covers "I'll pay you next week" without stretching so
#: far that every old invoice becomes a candidate.
DEFAULT_WINDOW = timedelta(days=10)

#: Two payments this close together, for the same money from the same payer, are
#: one payment entered twice far more often than they are two real purchases.
DUPLICATE_WINDOW = timedelta(hours=24)


@dataclass
class DeterministicMatcher:
    ledger: Ledger
    window: timedelta = DEFAULT_WINDOW
    duplicate_window: timedelta = DUPLICATE_WINDOW

    #: order reference -> the payment that already settled it
    _claimed: dict[str, str] = field(default_factory=dict, repr=False)
    #: (amount, payer fingerprint) -> (payment reference, when)
    _seen: dict[tuple[int, str], list[TxnRow]] = field(default_factory=dict, repr=False)

    def match_all(self, transactions: list[TxnRow]) -> list[Match]:
        """Work through a batch oldest first, so "already seen" means something."""
        ordered = sorted(transactions, key=lambda t: (t.paid_at, t.reference))
        return [self.match(txn) for txn in ordered]

    def match(self, txn: TxnRow) -> Match:
        for rule in (
            self._already_seen,
            self._exact_reference,
            self._dedicated_account,
            self._amount_and_window,
        ):
            result = rule(txn)
            if result is not None:
                self._remember(txn, result)
                return result

        self._remember(txn, None)
        return unresolved(
            txn.reference,
            "no certain match",
            txn.amount,
            channel=txn.channel,
            narration=txn.narration,
        )

    # ---------------------------------------------------------------- state

    def _fingerprint(self, txn: TxnRow) -> tuple[int, str]:
        who = names.normalise(txn.payer_name) or names.normalise(txn.narration)
        return (txn.amount.kobo, who)

    def _remember(self, txn: TxnRow, result: Match | None) -> None:
        self._seen.setdefault(self._fingerprint(txn), []).append(txn)
        if result is not None:
            for reference in result.order_references:
                self._claimed.setdefault(reference, txn.reference)

    # ---------------------------------------------------------------- rules

    def _already_seen(self, txn: TxnRow) -> Match | None:
        """The same money, from the same payer, twice in a day."""
        earlier = [
            other
            for other in self._seen.get(self._fingerprint(txn), ())
            if other.reference != txn.reference
            and timedelta() <= txn.paid_at - other.paid_at <= self.duplicate_window
        ]
        if not earlier:
            return None

        original = earlier[-1]
        # A repeat only counts as a duplicate if the first one actually settled
        # something. Two unmatched payments of the same size are still two
        # payments, and a person should look at both.
        settled_by_original = [
            reference for reference, by in self._claimed.items() if by == original.reference
        ]
        if not settled_by_original:
            return None

        return Match(
            transaction_reference=txn.reference,
            order_references=(),
            layer=Layer.EXACT_REFERENCE,
            confidence=0.95,
            evidence={
                "rule": "already_seen",
                "duplicate_of": original.reference,
                "minutes_apart": int((txn.paid_at - original.paid_at).total_seconds() // 60),
                "settled_by_original": settled_by_original,
                "note": "same amount and payer within a day; pays no new invoice",
            },
            # Always a person's call: this is either money to give back or a
            # genuine second purchase, and the difference is not in the data.
            needs_human=True,
            money_at_risk=txn.amount,
        )

    def _exact_reference(self, txn: TxnRow) -> Match | None:
        """An invoice number we can look up, either structured or in the text."""
        candidates: list[tuple[str, str]] = []

        if self.ledger.knows_reference(txn.stated_reference):
            assert txn.stated_reference is not None
            candidates.append((txn.stated_reference, "stated_reference"))

        for found in self._references_in(txn.narration):
            candidates.append((found, "narration"))

        unique = {reference for reference, _ in candidates}
        if len(unique) != 1:
            # Zero means nothing to go on. More than one means the narration
            # mentions two invoices, which is a split payment and not certain.
            return None

        reference = unique.pop()
        source = next(src for ref, src in candidates if ref == reference)
        order = self.ledger.orders[reference]

        return Match(
            transaction_reference=txn.reference,
            order_references=(reference,),
            layer=Layer.EXACT_REFERENCE,
            confidence=1.0,
            evidence={
                "rule": "exact_reference",
                "found_in": source,
                "order_amount_kobo": order.amount.kobo,
                "paid_amount_kobo": txn.amount.kobo,
                "amount_matches": order.amount == txn.amount,
            },
            money_at_risk=txn.amount,
        )

    def _references_in(self, text: str) -> list[str]:
        """Pull invoice-shaped tokens out of free text, keeping only real ones."""
        found: list[str] = []
        for raw in REFERENCE_PATTERN.findall(text or ""):
            normalised = raw.upper().replace("/", "-").replace(" ", "-")
            if self.ledger.knows_reference(normalised):
                found.append(normalised)
        return found

    def _dedicated_account(self, txn: TxnRow) -> Match | None:
        """Money in a customer's own account number tells us whose it is.

        Whose, not which. It only closes the case when that customer has exactly
        one outstanding invoice for exactly this amount. Otherwise we know the
        customer and still have to pick an invoice, and picking is guessing.
        """
        customer = self.ledger.customer_for_account(txn.dva_account_number)
        if customer is None:
            return None

        open_orders = [
            order
            for order in self.ledger.orders_for_customer(customer.id)
            if order.reference not in self._claimed and self._within_window(order, txn)
        ]
        exact = [order for order in open_orders if order.amount == txn.amount]

        if len(exact) != 1:
            return None

        order = exact[0]
        return Match(
            transaction_reference=txn.reference,
            order_references=(order.reference,),
            layer=Layer.DVA_ATTRIBUTION,
            confidence=1.0,
            evidence={
                "rule": "dedicated_account",
                "account_number": txn.dva_account_number,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "open_orders_considered": len(open_orders),
                "exact_amount_matches": 1,
            },
            money_at_risk=txn.amount,
        )

    def _amount_and_window(self, txn: TxnRow) -> Match | None:
        """One unpaid invoice, this exact amount, issued recently. Nothing else."""
        candidates = [
            order
            for order in self.ledger.orders_worth(txn.amount)
            if order.reference not in self._claimed and self._within_window(order, txn)
        ]
        if len(candidates) != 1:
            return None

        order = candidates[0]
        return Match(
            transaction_reference=txn.reference,
            order_references=(order.reference,),
            layer=Layer.AMOUNT_WINDOW,
            confidence=1.0,
            evidence={
                "rule": "amount_and_window",
                "amount_kobo": txn.amount.kobo,
                "issued_at": order.issued_at.isoformat(),
                "days_to_payment": (txn.paid_at - order.issued_at).days,
                "other_invoices_at_this_amount": len(self.ledger.orders_worth(txn.amount)) - 1,
            },
            money_at_risk=txn.amount,
        )

    def _within_window(self, order: OrderRow, txn: TxnRow) -> bool:
        """Paid after it was issued, and not so long after that anything fits."""
        gap = txn.paid_at - order.issued_at
        return timedelta(hours=-12) <= gap <= self.window


def coverage(matches: list[Match]) -> dict[str, float]:
    """What share of the batch each rule closed. This is the baseline number.

    Unrounded on purpose: the shares have to add up to exactly 1 so a report can
    be checked, and rounding here would lose the last kobo of the percentage.
    Round it where you print it.
    """
    total = len(matches) or 1
    counts: dict[str, int] = {}
    for match in matches:
        rule = str(match.evidence.get("rule", "unresolved"))
        counts[rule] = counts.get(rule, 0) + 1
    return {rule: count / total for rule, count in sorted(counts.items())}


def residual(matches: list[Match]) -> list[str]:
    """The payments this layer could not close, for the next layer to pick up."""
    return [m.transaction_reference for m in matches if m.layer is Layer.UNRESOLVED]
