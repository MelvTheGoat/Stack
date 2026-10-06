"""Reading the business's own books out of the database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from recon import state
from recon.db import session_scope
from recon.enums import (
    DETERMINISTIC_LAYERS,
    Channel,
    DecisionStatus,
    Layer,
    OrderStatus,
    RejectReason,
    TransactionStatus,
)
from recon.models import (
    Customer,
    DedicatedAccount,
    MatchDecision,
    Order,
    Settlement,
    Transaction,
)

pytestmark = pytest.mark.usefixtures("own_books")

FIXTURES = Path("fixtures")
WHEN = datetime(2024, 5, 17, 12, 0, tzinfo=UTC)


def _read(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((FIXTURES / name).read_text())
    return rows


def load_corpus(session: Session) -> None:
    """The practice corpus, written into the database the way an upload would.

    Every invoice goes in as open. The corpus records where each invoice ended
    up after the month's payments; an upload records where it stood before
    them, which is what the matcher needs to see.
    """
    for row in _read("customers.json"):
        session.add(Customer(id=row["id"], name=row["name"], phone=row.get("phone")))
    for row in _read("orders.json"):
        session.add(
            Order(
                reference=row["reference"],
                customer_id=row["customer_id"],
                amount_kobo=row["amount_kobo"],
                issued_at=datetime.fromisoformat(row["issued_at"]),
                status=OrderStatus.OPEN,
                description=row.get("description", ""),
            )
        )
    for row in _read("dedicated_accounts.json"):
        session.add(
            DedicatedAccount(
                account_number=row["account_number"],
                bank=row["bank"],
                customer_id=row["customer_id"],
            )
        )
    for row in _read("settlements.json"):
        session.add(
            Settlement(
                id=row["id"],
                settled_at=datetime.fromisoformat(row["settled_at"]),
                gross_kobo=row["gross_kobo"],
                fees_kobo=row["fees_kobo"],
                net_kobo=row["net_kobo"],
            )
        )
    for row in _read("transactions.json"):
        session.add(
            Transaction(
                reference=row["reference"],
                channel=Channel(row["channel"]),
                status=TransactionStatus(row["status"]),
                amount_kobo=row["amount_kobo"],
                fees_kobo=row["fees_kobo"],
                refunded_kobo=row.get("refunded_kobo", 0),
                paid_at=datetime.fromisoformat(row["paid_at"]),
                narration=row.get("narration", ""),
                payer_name=row.get("payer_name", ""),
                stated_reference=row.get("stated_reference"),
                dva_account_number=row.get("dva_account_number"),
                verified=bool(row.get("verified")),
                settlement_id=row.get("settlement_id"),
            )
        )


def add_invoice(session: Session, reference: str, naira_kobo: int, customer: str = "Ada") -> None:
    if session.get(Customer, customer) is None:
        session.add(Customer(id=customer, name=f"{customer} Okonkwo"))
    session.add(
        Order(
            reference=reference,
            customer_id=customer,
            amount_kobo=naira_kobo,
            issued_at=WHEN.replace(day=15),
        )
    )


def add_payment(session: Session, reference: str, kobo: int, narration: str = "") -> None:
    session.add(
        Transaction(
            reference=reference,
            channel=Channel.BANK_TRANSFER,
            status=TransactionStatus.SUCCESS,
            amount_kobo=kobo,
            paid_at=WHEN,
            narration=narration,
            payer_name="ADA OKONKWO",
            verified=True,
        )
    )


class TestAnEmptyStart:
    def test_nothing_in_the_books_is_an_empty_queue_not_an_error(self) -> None:
        space = state.workspace()
        assert not space.demo
        assert space.transactions == []
        assert space.queue.items == []

    def test_an_empty_day_still_balances(self) -> None:
        from recon.report import settlement

        space = state.workspace()
        report = settlement.build(space.last_trading_day(), space.transactions, space.matches)
        assert report.balances


class TestYourOwnBooks:
    def test_a_payment_naming_its_invoice_is_closed_without_a_person(self) -> None:
        with session_scope() as session:
            add_invoice(session, "INV-0042", 1_250_000)
            add_payment(session, "STMT-1", 1_250_000, narration="PAYMENT FOR INV-0042")

        space = state.workspace()
        (match,) = space.matches
        assert match.layer is Layer.EXACT_REFERENCE
        assert match.order_references == ("INV-0042",)
        assert space.queue.items == []

    def test_a_paid_or_voided_invoice_is_not_offered(self) -> None:
        with session_scope() as session:
            add_invoice(session, "INV-1", 500_000)
            session.flush()
            session.get(Order, "INV-1").status = OrderStatus.PAID  # type: ignore[union-attr]
            add_payment(session, "STMT-1", 500_000, narration="INV-1")

        (match,) = state.workspace().matches
        assert match.needs_human, "that invoice was already settled before this payment"

    def test_a_change_is_seen_on_the_next_read(self) -> None:
        assert state.workspace().transactions == []
        with session_scope() as session:
            add_payment(session, "STMT-1", 500_000)
        state.changed()
        assert [t.reference for t in state.workspace().transactions] == ["STMT-1"]

    def test_without_a_change_the_built_books_are_reused(self) -> None:
        first = state.workspace()
        assert state.workspace() is first

    def test_a_persons_decision_takes_the_payment_off_the_queue(self) -> None:
        with session_scope() as session:
            add_payment(session, "STMT-1", 500_000, narration="FROM A STRANGER")
        assert len(state.workspace().queue.items) == 1

        with session_scope() as session:
            session.add(
                MatchDecision(
                    transaction_reference="STMT-1",
                    layer=Layer.HUMAN,
                    status=DecisionStatus.REJECTED,
                    reject_reason=RejectReason.NOT_A_PAYMENT,
                    decided_by="ada",
                )
            )
        state.changed()

        space = state.workspace()
        assert space.queue.items == []
        (match,) = space.matches
        assert match.layer is Layer.HUMAN
        assert match.evidence["decided_by"] == "ada"


class TestTheCorpusReadFromTheDatabase:
    """The practice corpus, loaded into the database, must get exactly the same
    certain answers as when it is read from files. The only difference is the
    model: on your own books it suggests, and a person decides."""

    def test_layer_one_gives_the_same_answers_and_the_model_clears_nothing(self) -> None:
        demo = state.load()
        with session_scope() as session:
            load_corpus(session)
        own = state.from_database()

        demo_by_payment = {m.transaction_reference: m for m in demo.matches}
        own_by_payment = {m.transaction_reference: m for m in own.matches}
        assert demo_by_payment.keys() == own_by_payment.keys()

        for reference, theirs in demo_by_payment.items():
            ours = own_by_payment[reference]
            if theirs.layer in DETERMINISTIC_LAYERS:
                assert (ours.layer, ours.order_references) == (
                    theirs.layer,
                    theirs.order_references,
                ), reference
            assert ours.layer is not Layer.PROBABILISTIC

        cleared_by_the_model = sum(1 for m in demo.matches if m.layer is Layer.PROBABILISTIC)
        assert cleared_by_the_model > 0
        assert len(own.queue.items) == len(demo.queue.items) + cleared_by_the_model

    def test_the_settlement_batches_come_back_with_their_counts(self) -> None:
        with session_scope() as session:
            load_corpus(session)
        own = state.from_database()
        demo = state.load()
        assert sorted(own.settlements, key=lambda b: b["id"]) == sorted(
            [
                {**b, "settled_at": datetime.fromisoformat(b["settled_at"]).isoformat()}
                for b in demo.settlements
            ],
            key=lambda b: b["id"],
        )
