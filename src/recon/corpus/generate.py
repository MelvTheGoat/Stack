"""Build a realistic month of payments, and the answer key that goes with it.

This is the most valuable file in the repo. Anyone can write a matcher; the
hard part is having something honest to test it against. So the corpus is
generated with the failure modes deliberately built in, and every transaction
is recorded in a ground-truth file that says which orders it really paid and
which kind of mess it is. That answer key is what lets the evaluation harness
report a number you can believe.

The messes, all of which are things that actually happen:

* the payer typed the invoice number in, and it is right there in the narration
* money landed in a customer's dedicated account, so we know whose it is
* the narration has a name in it, spelled differently from the invoice
* the name is reversed, shortened to initials, or has a letter missing
* somebody paid part of an invoice and promised the rest
* somebody paid a bit too much
* one transfer covers three invoices
* the same payment was submitted twice
* a refund, and a reversal
* cash, which nothing can verify
* two open invoices that fit the payment equally well
* two customers whose names are one letter apart
* the bank settlement arrives the next day and is net of fees

Run `make corpus` to rebuild `fixtures/`. It is deterministic: same seed, same
corpus, so the numbers in the README do not move on their own.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from recon.corpus import names, narrations
from recon.enums import Channel, TransactionStatus
from recon.fees import fee_for, settlement_date
from recon.money import Money, sum_money

CORPUS_END = date(2024, 5, 31)
CORPUS_DAYS = 30
DEFAULT_SEED = 20240517


@dataclass(frozen=True, slots=True)
class TruthEntry:
    """The answer key for one transaction."""

    transaction_reference: str
    order_references: list[str]
    case: str
    """Which kind of mess this is. Groups the evaluation report."""

    winnable_by: str
    """The cheapest layer that could get this right:
    exact_reference, dva, amount_window, fuzzy, human, or none."""

    name_mangling: str = ""
    alternatives: bool = False
    """When true, `order_references` are alternatives, not a set.

    The twin-invoice cases have two invoices that fit equally well. Getting
    either one is as right as anyone can be, so scoring them as "must name
    both" would mark a correct answer wrong.
    """

    duplicate_of: str | None = None
    """For a double submission, the reference of the payment this repeats.

    Structured rather than buried in `note`, because the evaluation harness has
    to look it up and prose is not an index.
    """

    note: str = ""


@dataclass
class Corpus:
    customers: list[dict[str, Any]] = field(default_factory=list)
    dedicated_accounts: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    settlements: list[dict[str, Any]] = field(default_factory=list)
    truth: list[TruthEntry] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.corpus = Corpus()
        self._order_counter = 0
        self._txn_counter = 0

    # ------------------------------------------------------------- helpers

    def _moment(self, day: date) -> datetime:
        """A plausible time of day. Nigerian SMEs take money from about 8am to 9pm."""
        hour = self.rng.choices(
            population=list(range(8, 22)),
            weights=[3, 5, 7, 8, 9, 7, 6, 8, 9, 8, 7, 5, 3, 2],
            k=1,
        )[0]
        return datetime(
            day.year,
            day.month,
            day.day,
            hour,
            self.rng.randrange(60),
            self.rng.randrange(60),
            tzinfo=UTC,
        )

    def _phone(self) -> str:
        prefix = self.rng.choice((70, 80, 81, 90, 91))
        return f"0{prefix}{self.rng.randrange(10**8):08d}"

    def _order_ref(self) -> str:
        self._order_counter += 1
        return f"INV-{self._order_counter:04d}"

    def _txn_ref(self, channel: Channel) -> str:
        self._txn_counter += 1
        if channel is Channel.CARD:
            return f"STK-{self._txn_counter:05d}-{self.rng.randrange(0x1000, 0xFFFF):04X}"
        if channel is Channel.CASH:
            return f"cash-{self._txn_counter:05d}"
        return f"{self.rng.randrange(10**12, 10**13)}"

    def _price(self) -> Money:
        """SME invoice sizes: mostly a few thousand naira, occasionally much more."""
        band = self.rng.random()
        if band < 0.55:
            naira = self.rng.randrange(1_500, 25_000)
        elif band < 0.9:
            naira = self.rng.randrange(25_000, 120_000)
        else:
            naira = self.rng.randrange(120_000, 850_000)
        # Real prices land on round-ish numbers, with the odd exact kobo amount.
        if self.rng.random() < 0.85:
            naira = naira - (naira % 50)
        kobo = 0 if self.rng.random() < 0.9 else self.rng.choice((50, 99, 25))
        return Money(naira * 100 + kobo)

    # ------------------------------------------------------------ entities

    def build_customers(self, count: int = 45) -> None:
        seen: set[str] = set()
        while len(self.corpus.customers) < count:
            name = names.full_name(self.rng)
            if name in seen:
                continue
            seen.add(name)
            index = len(self.corpus.customers) + 1
            self.corpus.customers.append(
                {
                    "id": f"CUS_{index:03d}",
                    "name": name,
                    "phone": self._phone(),
                    "email": f"{name.split()[0].lower()}.{name.split()[1].lower()}@example.com",
                }
            )
        self._add_confusable_pairs()

    def _add_confusable_pairs(self) -> None:
        """Deliberate near-collisions. This is the adversarial slice.

        Two customers whose names differ by one letter, or who share a surname
        and a first initial, are the case where a fuzzy matcher is confidently
        wrong. The corpus has to contain them or the reported precision is a
        lie.
        """
        pairs = [
            ("Chinedu Okafor", "Chinedum Okafor"),
            ("Aisha Bello", "Aishat Bello"),
            ("Mohammed Sanusi", "Muhammad Sanusi"),
            ("Ngozi Eze", "Ngozi Ezeh"),
            ("Tunde Balogun", "Tunde Balogon"),
            ("Ifeanyi Obi", "Ifeanyi Obiora"),
        ]
        for left, right in pairs:
            for name in (left, right):
                index = len(self.corpus.customers) + 1
                first, last = name.split()[0], name.split()[-1]
                self.corpus.customers.append(
                    {
                        "id": f"CUS_{index:03d}",
                        "name": name,
                        "phone": self._phone(),
                        "email": f"{first.lower()}.{last.lower()}@example.com",
                        "confusable_with": right if name == left else left,
                    }
                )

    def build_dedicated_accounts(self, share: float = 0.4) -> None:
        """Give some customers a dedicated virtual account.

        Not everyone: a real business rolls these out gradually, and the gap
        between customers who have one and customers who do not is exactly the
        gap the fuzzy layer has to cover.
        """
        chosen = self.rng.sample(self.corpus.customers, k=int(len(self.corpus.customers) * share))
        for customer in chosen:
            self.corpus.dedicated_accounts.append(
                {
                    "account_number": f"99{self.rng.randrange(10**8):08d}",
                    "bank": self.rng.choice(("Wema Bank", "Titan Trust Bank", "Providus Bank")),
                    "customer_id": customer["id"],
                    "assigned_at": self._moment(
                        CORPUS_END - timedelta(days=CORPUS_DAYS + 5)
                    ).isoformat(),
                }
            )

    def _dva_for(self, customer_id: str) -> dict[str, Any] | None:
        for account in self.corpus.dedicated_accounts:
            if account["customer_id"] == customer_id:
                return account
        return None

    # -------------------------------------------------------------- orders

    def build_orders(self, count: int = 500) -> None:
        start = CORPUS_END - timedelta(days=CORPUS_DAYS)
        for _ in range(count):
            customer = self.rng.choice(self.corpus.customers)
            issued = start + timedelta(days=self.rng.randrange(CORPUS_DAYS + 1))
            self.corpus.orders.append(
                {
                    "reference": self._order_ref(),
                    "customer_id": customer["id"],
                    "amount_kobo": self._price().kobo,
                    "issued_at": self._moment(issued).isoformat(),
                    "due_at": self._moment(issued + timedelta(days=7)).isoformat(),
                    "status": "open",
                    "description": self.rng.choice(
                        (
                            "Ankara fabric, 6 yards",
                            "Generator servicing",
                            "Printing, 500 flyers",
                            "Catering deposit",
                            "Phone accessories, bulk",
                            "Hair products restock",
                            "Office chairs x4",
                            "Website maintenance, monthly",
                            "Bag of rice, 50kg",
                            "Solar inverter install",
                        )
                    ),
                }
            )

    def _twin_orders(self) -> None:
        """Add pairs of same-customer, same-amount, same-day invoices.

        A payment for that amount fits either one, and no rule may pretend to
        know which. What the deterministic layer must do is decline: two
        candidates is not a certainty.

        What happens next is the interesting part. Marking *either* one paid
        leaves the books correct, because the customer owes for two identical
        things and has paid for one of them. So this is not a case that has to
        go to a person; it is a case the certain layer must refuse and the
        scoring layer may take. The answer key marks both references as
        alternatives, and getting either is right.
        """
        for _ in range(10):
            customer = self.rng.choice(self.corpus.customers)
            amount = self._price().kobo
            issued = CORPUS_END - timedelta(days=self.rng.randrange(5, CORPUS_DAYS))
            for _ in range(2):
                self.corpus.orders.append(
                    {
                        "reference": self._order_ref(),
                        "customer_id": customer["id"],
                        "amount_kobo": amount,
                        # Same day, so the payment cannot arrive between them.
                        # A pair where one invoice did not exist yet is not
                        # ambiguous, it is just ordered, and the matcher is
                        # right to pick the earlier one.
                        "issued_at": self._moment(issued).isoformat(),
                        "due_at": self._moment(issued + timedelta(days=7)).isoformat(),
                        "status": "open",
                        "description": "Repeat order, same price",
                        "twin": True,
                    }
                )

    # --------------------------------------------------------- transactions

    def _emit(
        self,
        *,
        channel: Channel,
        amount: Money,
        when: datetime,
        narration: str = "",
        payer_name: str = "",
        stated_reference: str | None = None,
        dva_account_number: str | None = None,
        status: TransactionStatus = TransactionStatus.SUCCESS,
        reference: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        reference = reference or self._txn_ref(channel)
        row: dict[str, Any] = {
            "reference": reference,
            "channel": str(channel),
            "status": str(status),
            "amount_kobo": amount.kobo,
            "fees_kobo": fee_for(channel, amount).kobo,
            "refunded_kobo": 0,
            "currency": "NGN",
            "paid_at": when.isoformat(),
            "narration": narration,
            "payer_name": payer_name,
            "stated_reference": stated_reference,
            "dva_account_number": dva_account_number,
            "verified": True,
            "settlement_id": None,
        }
        row.update(extra or {})
        self.corpus.transactions.append(row)
        return reference

    def _truth(
        self,
        reference: str,
        orders: list[str],
        case: str,
        winnable_by: str,
        mangling: str = "",
        note: str = "",
        duplicate_of: str | None = None,
        alternatives: bool = False,
    ) -> None:
        self.corpus.truth.append(
            TruthEntry(
                transaction_reference=reference,
                order_references=orders,
                case=case,
                winnable_by=winnable_by,
                name_mangling=mangling,
                alternatives=alternatives,
                duplicate_of=duplicate_of,
                note=note,
            )
        )

    def _customer(self, customer_id: str) -> dict[str, Any]:
        for customer in self.corpus.customers:
            if customer["id"] == customer_id:
                return customer
        raise KeyError(customer_id)

    def _paid_on(self, order: dict[str, Any]) -> datetime:
        """People pay somewhere between the same day and about a week later."""
        issued = datetime.fromisoformat(order["issued_at"])
        delay = self.rng.choices((0, 1, 2, 3, 5, 8), weights=(30, 25, 18, 12, 10, 5), k=1)[0]
        paid = issued + timedelta(days=delay)
        return min(
            paid, datetime(CORPUS_END.year, CORPUS_END.month, CORPUS_END.day, 21, 0, tzinfo=UTC)
        )

    #: How a month of payments actually breaks down. These weights are the
    #: single most important guess in the repo: change them and every coverage
    #: number in the README changes with them. They are written here, in the
    #: open, rather than buried in a loop.
    SCENARIOS: tuple[tuple[str, float], ...] = (
        ("card_with_reference", 0.22),
        ("dva_exact", 0.13),
        ("transfer_with_reference", 0.07),
        ("transfer_name_exact", 0.14),
        ("transfer_name_partial", 0.10),
        ("transfer_name_over", 0.05),
        ("dva_partial", 0.05),
        ("split_three_invoices", 0.04),
        ("cash", 0.08),
        ("never_paid", 0.12),
    )

    def build_transactions(self) -> None:
        remaining = [o for o in self.corpus.orders if not o.get("twin")]
        self.rng.shuffle(remaining)
        pool = {o["reference"]: o for o in remaining}
        kinds = [k for k, _ in self.SCENARIOS]
        weights = [w for _, w in self.SCENARIOS]

        for order in remaining:
            if order["reference"] not in pool:
                continue  # already swallowed by a split payment
            scenario = self.rng.choices(kinds, weights=weights, k=1)[0]
            handler = getattr(self, f"_scenario_{scenario}")
            handler(order, pool)

        self._scenario_twin_payments()
        self._scenario_duplicates()
        self._scenario_refund()
        self._scenario_reversal()
        self._scenario_money_from_nowhere()

    def _settle_order(self, order: dict[str, Any], paid: Money) -> None:
        owed = Money(order["amount_kobo"])
        if paid >= owed:
            order["status"] = "overpaid" if paid > owed else "paid"
        else:
            order["status"] = "part_paid"

    # ------------------------------------------------------- the scenarios

    def _scenario_card_with_reference(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """The easy case. Paid online, so our own order reference rides along."""
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        amount = Money(order["amount_kobo"])
        reference = self._emit(
            channel=Channel.CARD,
            amount=amount,
            when=self._paid_on(order),
            payer_name=customer["name"],
            stated_reference=order["reference"],
            narration=f"Card payment {order['reference']}",
        )
        self._settle_order(order, amount)
        self._truth(reference, [order["reference"]], "card_with_reference", "exact_reference")

    def _scenario_dva_exact(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """Money into the customer's own account number. No name matching needed."""
        account = self._dva_for(order["customer_id"])
        if account is None:
            return self._scenario_transfer_name_exact(order, pool)
        pool.pop(order["reference"], None)

        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        amount = Money(order["amount_kobo"])
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.DVA,
            amount=amount,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
            dva_account_number=account["account_number"],
        )
        self._settle_order(order, amount)
        self._truth(reference, [order["reference"]], "dva_exact", "dva", mangling)

    def _scenario_dva_partial(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """We know whose money it is, but not which invoice, and it is short."""
        account = self._dva_for(order["customer_id"])
        if account is None:
            return self._scenario_transfer_name_partial(order, pool)
        pool.pop(order["reference"], None)

        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        owed = Money(order["amount_kobo"])
        paid = Money(int(owed.kobo * self.rng.choice((0.4, 0.5, 0.6, 0.75))) // 100 * 100)
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.DVA,
            amount=paid,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
            dva_account_number=account["account_number"],
        )
        self._settle_order(order, paid)
        self._truth(
            reference,
            [order["reference"]],
            "dva_partial",
            "dva",
            mangling,
            note="right customer, wrong amount, so which invoice is a guess",
        )

    def _scenario_transfer_with_reference(
        self, order: dict[str, Any], pool: dict[str, Any]
    ) -> None:
        """The payer typed the invoice number into the narration. Bless them."""
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        amount = Money(order["amount_kobo"])
        text, _ = narrations.narration(self.rng, shown, order_ref=order["reference"])
        reference = self._emit(
            channel=Channel.BANK_TRANSFER,
            amount=amount,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
        )
        self._settle_order(order, amount)
        self._truth(
            reference, [order["reference"]], "transfer_with_reference", "exact_reference", mangling
        )

    def _scenario_transfer_name_exact(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """A name in a narration and an amount that matches to the kobo."""
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        amount = Money(order["amount_kobo"])
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.BANK_TRANSFER,
            amount=amount,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
        )
        self._settle_order(order, amount)
        self._truth(
            reference, [order["reference"]], "transfer_name_exact", "amount_window", mangling
        )

    def _scenario_transfer_name_partial(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """Underpaid. The amount no longer identifies anything."""
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        owed = Money(order["amount_kobo"])
        paid = Money(int(owed.kobo * self.rng.choice((0.3, 0.5, 0.6, 0.7, 0.8))) // 100 * 100)
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.BANK_TRANSFER,
            amount=paid,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
        )
        self._settle_order(order, paid)
        self._truth(
            reference,
            [order["reference"]],
            "transfer_name_partial",
            "fuzzy",
            mangling,
            note="part payment; the balance may never arrive",
        )

    def _scenario_transfer_name_over(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """Overpaid, usually by rounding up to a round number."""
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        owed = Money(order["amount_kobo"])
        rounded_up = ((owed.kobo // 100_000) + 1) * 100_000
        paid = Money(max(rounded_up, owed.kobo + self.rng.choice((50_000, 100_000, 20_000))))
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.BANK_TRANSFER,
            amount=paid,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
        )
        self._settle_order(order, paid)
        self._truth(
            reference,
            [order["reference"]],
            "transfer_name_over",
            "fuzzy",
            mangling,
            note="paid more than the invoice; the rest is credit",
        )

    def _scenario_split_three_invoices(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """One transfer settling three invoices at once. Sunday-evening behaviour."""
        siblings = [
            o
            for ref, o in pool.items()
            if o["customer_id"] == order["customer_id"] and ref != order["reference"]
        ][:2]
        if len(siblings) < 2:
            return self._scenario_transfer_name_exact(order, pool)

        group = [order, *siblings]
        for item in group:
            pool.pop(item["reference"], None)

        customer = self._customer(order["customer_id"])
        shown, mangling = names.mangle(customer["name"], self.rng)
        total = sum_money([Money(o["amount_kobo"]) for o in group])
        text, _ = narrations.narration(self.rng, shown)
        reference = self._emit(
            channel=Channel.BANK_TRANSFER,
            amount=total,
            when=self._paid_on(order),
            narration=text,
            payer_name=shown,
        )
        for item in group:
            item["status"] = "paid"
        self._truth(
            reference,
            [o["reference"] for o in group],
            "split_three_invoices",
            "fuzzy",
            mangling,
            note="one payment, three invoices; only the total matches anything",
        )

    def _scenario_cash(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """Someone handed over notes and a person typed it in.

        Nothing here is verifiable. There is no Paystack record, no bank record,
        no signature. The system's honest position is that it takes the human's
        word for it and marks the row as attested rather than verified.
        """
        pool.pop(order["reference"], None)
        customer = self._customer(order["customer_id"])
        amount = Money(order["amount_kobo"])
        mentions_reference = self.rng.random() < 0.45

        # The name on a cash receipt is whatever the person at the counter
        # wrote, so it is mangled like any other. Recording the customer's real
        # name here would hand the matcher the answer and flatter every number.
        shown, mangling = names.mangle(customer["name"], self.rng)
        shown = shown.title()
        note = (
            f"Cash from {shown} for {order['reference']}"
            if mentions_reference
            else f"Cash received from {shown}"
        )
        reference = self._emit(
            channel=Channel.CASH,
            amount=amount,
            when=self._paid_on(order),
            narration=note,
            payer_name=shown,
            stated_reference=order["reference"] if mentions_reference else None,
            extra={
                "verified": False,
                "attested_by": self.rng.choice(("shop_floor", "manager", "owner")),
            },
        )
        self._settle_order(order, amount)
        self._truth(
            reference,
            [order["reference"]],
            "cash",
            "exact_reference" if mentions_reference else "fuzzy",
            mangling,
            note="attested by a human; nothing verifies it",
        )

    def _scenario_never_paid(self, order: dict[str, Any], pool: dict[str, Any]) -> None:
        """Some invoices just do not get paid. The matcher must not invent a payment."""
        pool.pop(order["reference"], None)
        order["status"] = "open"

    # -------------------------------------------------- the adversarial bits

    def _scenario_twin_payments(self) -> None:
        """Pay one of two identical invoices. There is no way to tell which."""
        twins = [o for o in self.corpus.orders if o.get("twin")]
        for index in range(0, len(twins) - 1, 2):
            first, second = twins[index], twins[index + 1]
            customer = self._customer(first["customer_id"])
            shown, mangling = names.mangle(customer["name"], self.rng)
            amount = Money(first["amount_kobo"])
            text, _ = narrations.narration(self.rng, shown)
            reference = self._emit(
                channel=Channel.BANK_TRANSFER,
                amount=amount,
                when=self._paid_on(first),
                narration=text,
                payer_name=shown,
            )
            # The truth is genuinely "one of these two", and either is right.
            first["status"] = "paid"
            self._truth(
                reference,
                [first["reference"], second["reference"]],
                "ambiguous_twin_invoices",
                "fuzzy",
                mangling,
                alternatives=True,
                note=(
                    "two identical open invoices for the same customer; either "
                    "one may be marked paid and the books are still right, but "
                    "no rule may claim to know which"
                ),
            )

    def _scenario_duplicates(self, count: int = 12) -> None:
        """The same payment reported twice.

        Two flavours, and they are different problems. A webhook replay carries
        the same reference and the ingest layer drops it. A genuine double
        submission — the customer pressed pay twice, or someone re-entered a
        cash receipt — carries a *different* reference for the same money, and
        only the matcher can catch it.
        """
        candidates = [
            t
            for t in self.corpus.transactions
            if t["channel"] in ("bank_transfer", "cash") and t["status"] == "success"
        ]
        for original in self.rng.sample(candidates, k=min(count, len(candidates))):
            truth = next(
                t for t in self.corpus.truth if t.transaction_reference == original["reference"]
            )
            when = datetime.fromisoformat(original["paid_at"]) + timedelta(
                minutes=self.rng.randrange(2, 90)
            )
            reference = self._emit(
                channel=Channel(original["channel"]),
                amount=Money(original["amount_kobo"]),
                when=when,
                narration=original["narration"],
                payer_name=original["payer_name"],
                stated_reference=original["stated_reference"],
                dva_account_number=original["dva_account_number"],
            )
            self._truth(
                reference,
                [],
                "duplicate_submission",
                "human",
                truth.name_mangling,
                duplicate_of=original["reference"],
                note="the same money submitted again under a fresh reference",
            )

    def _scenario_refund(self, count: int = 6) -> None:
        """Money given back. The order goes back to open."""
        candidates = [
            t
            for t in self.corpus.transactions
            if t["channel"] == "card" and t["status"] == "success"
        ]
        for txn in self.rng.sample(candidates, k=min(count, len(candidates))):
            txn["status"] = str(TransactionStatus.REFUNDED)
            txn["refunded_kobo"] = txn["amount_kobo"]
            truth = next(
                t for t in self.corpus.truth if t.transaction_reference == txn["reference"]
            )
            for reference in truth.order_references:
                self._order(reference)["status"] = "open"

    def _scenario_reversal(self, count: int = 3) -> None:
        """A transfer that was pulled back by the sending bank. Never ours."""
        candidates = [
            t
            for t in self.corpus.transactions
            if t["channel"] == "bank_transfer" and t["status"] == "success"
        ]
        for txn in self.rng.sample(candidates, k=min(count, len(candidates))):
            txn["status"] = str(TransactionStatus.REVERSED)
            truth = next(
                t for t in self.corpus.truth if t.transaction_reference == txn["reference"]
            )
            for reference in truth.order_references:
                self._order(reference)["status"] = "open"

    def _scenario_money_from_nowhere(self, count: int = 10) -> None:
        """Payments that match no invoice at all.

        A relative sending money to the owner's business account, a deposit for
        an order that was never written up, a wrong-account transfer. These are
        the cases where the correct answer is "no match", and a matcher scored
        only on the payments that do match will never be tested on them.
        """
        for _ in range(count):
            stranger = names.full_name(self.rng)
            shown, mangling = names.mangle(stranger, self.rng)
            text, _ = narrations.narration(self.rng, shown)
            day = CORPUS_END - timedelta(days=self.rng.randrange(CORPUS_DAYS))
            reference = self._emit(
                channel=Channel.BANK_TRANSFER,
                amount=self._price(),
                when=self._moment(day),
                narration=text,
                payer_name=shown,
            )
            self._truth(
                reference,
                [],
                "no_matching_order",
                "none",
                mangling,
                note="real money, no invoice; the right answer is to leave it alone",
            )

    def _order(self, reference: str) -> dict[str, Any]:
        for order in self.corpus.orders:
            if order["reference"] == reference:
                return order
        raise KeyError(reference)

    # --------------------------------------------------------- settlements

    def build_settlements(self) -> None:
        """Group what Paystack owes us into the batches it actually pays.

        Cash never settles: it is already in the till. Main-account transfers
        never settle either, because they went straight to the bank and Paystack
        was not involved. Only card and dedicated-account money goes through a
        settlement batch, net of fees, on the next working day. That is the
        timing gap that makes the bank statement disagree with the day's sales.
        """
        batches: dict[str, list[dict[str, Any]]] = {}
        for txn in self.corpus.transactions:
            channel = Channel(txn["channel"])
            if channel not in (Channel.CARD, Channel.DVA):
                continue
            if txn["status"] not in ("success", "refunded"):
                continue
            paid_on = datetime.fromisoformat(txn["paid_at"]).date()
            lands_on = settlement_date(paid_on, channel)
            batches.setdefault(lands_on.isoformat(), []).append(txn)

        for day in sorted(batches):
            members = batches[day]
            batch_id = f"STL_{day.replace('-', '')}"
            gross = sum_money([Money(t["amount_kobo"]) for t in members])
            fees = sum_money([Money(t["fees_kobo"]) for t in members])
            refunds = sum_money([Money(t["refunded_kobo"]) for t in members])
            for txn in members:
                txn["settlement_id"] = batch_id
            self.corpus.settlements.append(
                {
                    "id": batch_id,
                    "settled_at": f"{day}T14:00:00+00:00",
                    "gross_kobo": gross.kobo,
                    "fees_kobo": fees.kobo,
                    "net_kobo": (gross - fees - refunds).kobo,
                    "transaction_count": len(members),
                }
            )

    # ------------------------------------------------------------ manifest

    def build_manifest(self) -> None:
        by_case: dict[str, int] = {}
        by_winnable: dict[str, int] = {}
        for entry in self.corpus.truth:
            by_case[entry.case] = by_case.get(entry.case, 0) + 1
            by_winnable[entry.winnable_by] = by_winnable.get(entry.winnable_by, 0) + 1

        by_channel: dict[str, int] = {}
        for txn in self.corpus.transactions:
            by_channel[txn["channel"]] = by_channel.get(txn["channel"], 0) + 1

        self.corpus.manifest = {
            "seed": self.seed,
            "generated_for_period": {
                "from": (CORPUS_END - timedelta(days=CORPUS_DAYS)).isoformat(),
                "to": CORPUS_END.isoformat(),
            },
            "counts": {
                "customers": len(self.corpus.customers),
                "dedicated_accounts": len(self.corpus.dedicated_accounts),
                "orders": len(self.corpus.orders),
                "transactions": len(self.corpus.transactions),
                "settlements": len(self.corpus.settlements),
            },
            "transactions_by_channel": dict(sorted(by_channel.items())),
            "cases": dict(sorted(by_case.items())),
            "best_possible_layer": dict(sorted(by_winnable.items())),
        }


def build(seed: int = DEFAULT_SEED) -> Corpus:
    """Build the whole corpus. Same seed in, same corpus out."""
    builder = _Builder(seed)
    builder.build_customers()
    builder.build_dedicated_accounts()
    builder.build_orders()
    builder._twin_orders()
    builder.build_transactions()
    builder.build_settlements()
    builder.build_manifest()
    return builder.corpus


def write(corpus: Corpus, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {
        "customers.json": corpus.customers,
        "dedicated_accounts.json": corpus.dedicated_accounts,
        "orders.json": corpus.orders,
        "transactions.json": corpus.transactions,
        "settlements.json": corpus.settlements,
        "ground_truth.json": [asdict(t) for t in corpus.truth],
        "manifest.json": corpus.manifest,
    }
    for name, payload in files.items():
        (directory / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("fixtures"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    corpus = build(args.seed)
    write(corpus, args.out)
    print(json.dumps(corpus.manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
