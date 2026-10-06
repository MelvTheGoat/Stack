"""The book of who owes what, loaded once and indexed for lookup.

The matcher asks the same handful of questions over and over — is this string a
real order reference, whose dedicated account is this, which open orders does
this customer have, which orders are for exactly this amount — so they get
indexes instead of a scan each time.

Nothing in here decides anything. It is only the lookup surface.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from recon.corpus import names
from recon.money import Money


@dataclass(frozen=True, slots=True)
class OrderRow:
    reference: str
    customer_id: str
    amount: Money
    issued_at: datetime
    status: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class CustomerRow:
    id: str
    name: str
    phone: str = ""
    email: str = ""

    @property
    def normalised_name(self) -> str:
        return names.normalise(self.name)

    @property
    def name_tokens(self) -> frozenset[str]:
        return frozenset(names.name_tokens(self.name))


@dataclass(frozen=True, slots=True)
class TxnRow:
    """A payment, exactly as the matcher gets to see it.

    Deliberately does not carry a customer id. If it did, there would be nothing
    to match.
    """

    reference: str
    channel: str
    status: str
    amount: Money
    fees: Money
    paid_at: datetime
    refunded: Money = field(default_factory=Money.zero)
    narration: str = ""
    payer_name: str = ""
    stated_reference: str | None = None
    dva_account_number: str | None = None
    verified: bool = False
    settlement_id: str | None = None


@dataclass
class Ledger:
    customers: dict[str, CustomerRow] = field(default_factory=dict)
    orders: dict[str, OrderRow] = field(default_factory=dict)
    dva_to_customer: dict[str, str] = field(default_factory=dict)

    _orders_by_customer: dict[str, list[OrderRow]] = field(default_factory=dict, repr=False)
    _orders_by_amount: dict[int, list[OrderRow]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------- loading

    @classmethod
    def from_fixtures(cls, directory: Path) -> Ledger:
        ledger = cls()
        ledger.add_customers(_read(directory / "customers.json"))
        ledger.add_orders(_read(directory / "orders.json"))
        ledger.add_dedicated_accounts(_read(directory / "dedicated_accounts.json"))
        return ledger

    def add_customers(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.customers[row["id"]] = CustomerRow(
                id=row["id"],
                name=row["name"],
                phone=row.get("phone", ""),
                email=row.get("email", ""),
            )

    def add_orders(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            order = OrderRow(
                reference=row["reference"],
                customer_id=row["customer_id"],
                amount=Money(row["amount_kobo"]),
                issued_at=datetime.fromisoformat(row["issued_at"]),
                status=row.get("status", "open"),
                description=row.get("description", ""),
            )
            self.orders[order.reference] = order
            self._orders_by_customer.setdefault(order.customer_id, []).append(order)
            self._orders_by_amount.setdefault(order.amount.kobo, []).append(order)

    def add_dedicated_accounts(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.dva_to_customer[row["account_number"]] = row["customer_id"]

    # ------------------------------------------------------------ lookups

    def customer_for_account(self, account_number: str | None) -> CustomerRow | None:
        if not account_number:
            return None
        customer_id = self.dva_to_customer.get(account_number)
        return self.customers.get(customer_id) if customer_id else None

    def orders_for_customer(self, customer_id: str) -> list[OrderRow]:
        return list(self._orders_by_customer.get(customer_id, ()))

    def orders_worth(self, amount: Money) -> list[OrderRow]:
        return list(self._orders_by_amount.get(amount.kobo, ()))

    def knows_reference(self, reference: str | None) -> bool:
        return bool(reference) and reference in self.orders


def _read(path: Path) -> list[dict[str, Any]]:
    payload: Any = json.loads(path.read_text())
    return list(payload) if isinstance(payload, list) else []


def transactions_from_fixtures(directory: Path) -> list[TxnRow]:
    rows = _read(directory / "transactions.json")
    return [
        TxnRow(
            reference=row["reference"],
            channel=row["channel"],
            status=row["status"],
            amount=Money(row["amount_kobo"]),
            fees=Money(row["fees_kobo"]),
            paid_at=datetime.fromisoformat(row["paid_at"]),
            refunded=Money(row.get("refunded_kobo", 0)),
            narration=row.get("narration", ""),
            payer_name=row.get("payer_name", ""),
            stated_reference=row.get("stated_reference"),
            dva_account_number=row.get("dva_account_number"),
            verified=bool(row.get("verified", False)),
            settlement_id=row.get("settlement_id"),
        )
        for row in rows
    ]
