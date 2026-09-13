"""Working out which invoices a payment could possibly be for.

Scoring every payment against every open invoice would be 400 x 500 comparisons
a night, most of them obviously silly. So this narrows the field first, cheaply,
and the model only ever sees a shortlist.

Three ways onto the shortlist:

1. the payment landed in a dedicated account, so that customer's invoices are in
2. somebody whose name looks like the name on the payment has open invoices
3. an invoice is for exactly this amount, whoever it belongs to

Plus combinations: two or three of one customer's invoices that add up to
exactly the amount paid. That is the "one transfer, three invoices" case, and no
single-invoice shortlist can ever catch it.

Narrowing can lose the right answer, and when it does the case ends up in the
review queue rather than matched wrongly. That is the trade we want.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations

from recon.match.ledger import CustomerRow, Ledger, OrderRow, TxnRow
from recon.match.similarity import name_similarity
from recon.money import Money, sum_money

#: How far back to look for an invoice a payment might belong to. Wider than the
#: deterministic window, because the fuzzy layer is allowed to be less sure.
LOOKBACK = timedelta(days=45)

#: A payment cannot be for an invoice that did not exist yet, give or take a
#: little clock skew between Paystack and our own records.
FORWARD_GRACE = timedelta(hours=12)

#: Below this, the name is not evidence of anything.
NAME_FLOOR = 0.40

MAX_CUSTOMERS = 8
MAX_CANDIDATES = 24
MAX_COMBINATION_SIZE = 3


@dataclass(frozen=True, slots=True)
class Candidate:
    """One thing a payment might be for: an invoice, or a small set of them."""

    order_references: tuple[str, ...]
    customer_id: str
    total: Money
    via: str
    """How it got onto the shortlist: dva, name, amount, or combination."""

    @property
    def is_combination(self) -> bool:
        return len(self.order_references) > 1


def observed_name(txn: TxnRow) -> str:
    """Everything we were told about who paid, as one string."""
    return f"{txn.payer_name} {txn.narration}".strip()


def shortlist(
    ledger: Ledger,
    txn: TxnRow,
    claimed: set[str],
    *,
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    text = observed_name(txn)
    found: dict[tuple[str, ...], Candidate] = {}

    def offer(orders: list[OrderRow], customer_id: str, via: str) -> None:
        key = tuple(sorted(o.reference for o in orders))
        if key in found:
            return
        found[key] = Candidate(
            order_references=key,
            customer_id=customer_id,
            total=sum_money([o.amount for o in orders]),
            via=via,
        )

    # 1. The dedicated account, if there is one. Strongest route in.
    dva_customer = ledger.customer_for_account(txn.dva_account_number)
    if dva_customer is not None:
        open_orders = _open_orders(ledger, dva_customer.id, txn, claimed)
        for order in open_orders:
            offer([order], dva_customer.id, "dva")
        for group in _combinations_summing_to(open_orders, txn.amount):
            offer(list(group), dva_customer.id, "combination")

    # 2. Customers whose name resembles whatever the payment said.
    for customer in _similar_customers(ledger, text):
        open_orders = _open_orders(ledger, customer.id, txn, claimed)
        for order in open_orders:
            offer([order], customer.id, "name")
        for group in _combinations_summing_to(open_orders, txn.amount):
            offer(list(group), customer.id, "combination")

    # 3. Anybody's invoice for exactly this amount.
    for order in ledger.orders_worth(txn.amount):
        if order.reference in claimed or not _in_lookback(order, txn):
            continue
        offer([order], order.customer_id, "amount")

    ranked = sorted(
        found.values(),
        key=lambda c: (-_prefilter_score(ledger, txn, c, text), c.order_references),
    )
    return ranked[:max_candidates]


def _open_orders(
    ledger: Ledger, customer_id: str, txn: TxnRow, claimed: set[str]
) -> list[OrderRow]:
    return [
        order
        for order in ledger.orders_for_customer(customer_id)
        if order.reference not in claimed and _in_lookback(order, txn)
    ]


def _in_lookback(order: OrderRow, txn: TxnRow) -> bool:
    gap = txn.paid_at - order.issued_at
    return -FORWARD_GRACE <= gap <= LOOKBACK


def _similar_customers(ledger: Ledger, text: str) -> list[CustomerRow]:
    if not text.strip():
        return []
    scored = [
        (name_similarity(text, customer.name), customer) for customer in ledger.customers.values()
    ]
    close_enough = [(score, c) for score, c in scored if score >= NAME_FLOOR]
    close_enough.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [customer for _, customer in close_enough[:MAX_CUSTOMERS]]


def _combinations_summing_to(orders: list[OrderRow], target: Money) -> list[tuple[OrderRow, ...]]:
    """Sets of two or three invoices that add up to exactly what was paid.

    Exactly, to the kobo. A near-miss combination is not evidence; it is
    arithmetic coincidence, and there are a lot of those in a list of prices.
    """
    if len(orders) < 2:
        return []
    # Guard the combinatorics: a customer with 20 open invoices would otherwise
    # produce over a thousand triples, and the honest answer for a customer in
    # that state is a human anyway.
    pool = sorted(orders, key=lambda o: o.issued_at)[:12]
    hits: list[tuple[OrderRow, ...]] = []
    for size in range(2, MAX_COMBINATION_SIZE + 1):
        for group in combinations(pool, size):
            if sum_money([o.amount for o in group]) == target:
                hits.append(group)
    return hits


def _prefilter_score(ledger: Ledger, txn: TxnRow, candidate: Candidate, text: str) -> float:
    """A rough ranking, only used to decide what makes the shortlist."""
    customer = ledger.customers.get(candidate.customer_id)
    name = name_similarity(text, customer.name) if customer else 0.0
    exact_amount = 1.0 if candidate.total == txn.amount else 0.0
    route = {"dva": 1.0, "combination": 0.7, "amount": 0.5, "name": 0.4}[candidate.via]
    return 2.0 * route + 1.5 * exact_amount + name
