from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from recon import audit
from recon.enums import Channel, DecisionStatus, Layer, TransactionStatus
from recon.models import Base, Customer, DedicatedAccount, Order, Transaction
from recon.money import Money


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_money_columns_come_back_as_money(session: Session) -> None:
    session.add(Customer(id="cus_1", name="Ada Okonkwo"))
    session.add(
        Transaction(
            reference="ref_1",
            channel=Channel.CARD,
            status=TransactionStatus.SUCCESS,
            amount_kobo=125050,
            fees_kobo=1976,
            paid_at=datetime(2024, 5, 17, tzinfo=UTC),
        )
    )
    session.commit()
    txn = session.get(Transaction, "ref_1")
    assert txn is not None
    assert txn.amount == Money(125050)
    assert str(txn.amount) == "₦1,250.50"
    assert txn.net == Money(125050 - 1976)


def test_net_takes_off_refunds_too(session: Session) -> None:
    txn = Transaction(
        reference="ref_r",
        channel=Channel.CARD,
        amount_kobo=100_000,
        fees_kobo=1_600,
        refunded_kobo=40_000,
        paid_at=datetime(2024, 5, 17, tzinfo=UTC),
    )
    session.add(txn)
    session.commit()
    assert txn.net == Money(58_400)


def test_one_decision_per_transaction(session: Session) -> None:
    from recon.models import MatchDecision

    session.add(
        Transaction(
            reference="ref_2",
            channel=Channel.DVA,
            amount_kobo=5000,
            paid_at=datetime(2024, 5, 17, tzinfo=UTC),
        )
    )
    session.commit()
    for _ in range(2):
        session.add(
            MatchDecision(
                transaction_reference="ref_2",
                layer=Layer.EXACT_REFERENCE,
                status=DecisionStatus.AUTO_CLEARED,
                confidence=1.0,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_dedicated_account_points_at_exactly_one_customer(session: Session) -> None:
    session.add(Customer(id="cus_2", name="Chinedu Eze"))
    session.add(
        DedicatedAccount(account_number="9912345678", bank="Wema Bank", customer_id="cus_2")
    )
    session.commit()
    account = session.get(DedicatedAccount, "9912345678")
    assert account is not None
    assert account.customer.name == "Chinedu Eze"


def test_order_amount_is_money(session: Session) -> None:
    session.add(Customer(id="cus_3", name="Bisi Adeyemi"))
    session.add(
        Order(
            reference="INV-001",
            customer_id="cus_3",
            amount_kobo=250_000,
            issued_at=datetime(2024, 5, 17, tzinfo=UTC),
        )
    )
    session.commit()
    order = session.get(Order, "INV-001")
    assert order is not None
    assert str(order.amount) == "₦2,500.00"


def test_audit_rows_are_written_in_order_and_keep_their_evidence(session: Session) -> None:
    audit.record(
        session,
        action="matched",
        subject_type="transaction",
        subject_id="ref_1",
        layer=Layer.EXACT_REFERENCE,
        confidence=1.0,
        inputs={"narration": "PAYMENT INV-001"},
        evidence={"matched_on": "stated_reference"},
    )
    audit.record(
        session,
        action="approved",
        subject_type="transaction",
        subject_id="ref_1",
        layer=Layer.HUMAN,
        actor="melvyn",
    )
    session.commit()

    rows = audit.history(session, "transaction", "ref_1")
    assert [r.action for r in rows] == ["matched", "approved"]
    assert rows[0].layer == Layer.EXACT_REFERENCE
    assert rows[1].actor == "melvyn"
    assert "stated_reference" in rows[0].evidence_json
