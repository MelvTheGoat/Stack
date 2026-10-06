"""The business's own books, read out of the database.

The demo reads a generated corpus from `fixtures/`. A real instance reads this
instead: the invoices someone uploaded, the payments Paystack told us about or a
bank statement listed, and every decision a person has already made. It all
comes out in the shapes the matcher and the report already take, so neither of
them knows or cares which of the two it is looking at.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from recon.enums import DecisionStatus, Layer, OrderStatus
from recon.match.ledger import CustomerRow, Ledger, OrderRow, TxnRow
from recon.match.result import Match
from recon.models import (
    Customer,
    DedicatedAccount,
    MatchDecision,
    Order,
    Settlement,
    Transaction,
)
from recon.money import Money

#: Invoices a payment could still be for. A paid or voided invoice stays in the
#: database for the record, but it is not offered to the matcher.
STILL_OWED: frozenset[OrderStatus] = frozenset({OrderStatus.OPEN, OrderStatus.PART_PAID})


def ledger(session: Session) -> Ledger:
    book = Ledger()
    for customer in session.query(Customer).all():
        book.customers[customer.id] = CustomerRow(
            id=customer.id,
            name=customer.name,
            phone=customer.phone or "",
            email=customer.email or "",
        )
    for order in session.query(Order).order_by(Order.issued_at, Order.reference).all():
        if order.status not in STILL_OWED:
            continue
        book.add_order(
            OrderRow(
                reference=order.reference,
                customer_id=order.customer_id,
                amount=order.amount,
                issued_at=order.issued_at,
                status=str(order.status),
                description=order.description,
            )
        )
    for account in session.query(DedicatedAccount).all():
        book.dva_to_customer[account.account_number] = account.customer_id
    return book


def transactions(session: Session) -> list[TxnRow]:
    return [
        TxnRow(
            reference=txn.reference,
            channel=str(txn.channel),
            status=str(txn.status),
            amount=txn.amount,
            fees=txn.fees,
            paid_at=txn.paid_at,
            refunded=Money(txn.refunded_kobo),
            narration=txn.narration,
            payer_name=txn.payer_name,
            stated_reference=txn.stated_reference,
            dva_account_number=txn.dva_account_number,
            verified=txn.verified,
            settlement_id=txn.settlement_id,
        )
        for txn in session.query(Transaction).order_by(Transaction.paid_at).all()
    ]


def decisions(session: Session) -> dict[str, Match]:
    """What a person has already said, as matches the pipeline will not redo.

    A "no" closes the payment as paying none of the suggested invoices, the same
    as it does on the demo. The reason stays on the decision row as a label.
    """
    rows = (
        session.query(MatchDecision)
        .filter(MatchDecision.status.in_([DecisionStatus.APPROVED, DecisionStatus.REJECTED]))
        .all()
    )
    return {
        row.transaction_reference: Match(
            transaction_reference=row.transaction_reference,
            order_references=tuple(row.order_references),
            layer=Layer.HUMAN,
            confidence=1.0,
            evidence={
                "rule": "human",
                "decision": str(row.status),
                "decided_by": row.decided_by,
                "reject_reason": str(row.reject_reason) if row.reject_reason else None,
            },
            money_at_risk=row.money_at_risk,
        )
        for row in rows
    }


def settlements(session: Session) -> list[dict[str, Any]]:
    members: dict[str, int] = {
        str(batch): int(count)
        for batch, count in session.query(Transaction.settlement_id, func.count())
        .filter(Transaction.settlement_id.is_not(None))
        .group_by(Transaction.settlement_id)
        .all()
    }
    return [
        {
            "id": batch.id,
            "settled_at": batch.settled_at.isoformat(),
            "gross_kobo": batch.gross_kobo,
            "fees_kobo": batch.fees_kobo,
            "net_kobo": batch.net_kobo,
            "transaction_count": members.get(batch.id, 0),
        }
        for batch in session.query(Settlement).order_by(Settlement.settled_at).all()
    ]


def counts(session: Session) -> dict[str, int]:
    """How much is in the books, for the setup page."""
    return {
        "customers": session.query(Customer).count(),
        "invoices": session.query(Order).count(),
        "invoices_owed": session.query(Order).filter(Order.status.in_(STILL_OWED)).count(),
        "payments": session.query(Transaction).count(),
        "decisions": session.query(MatchDecision).count(),
    }
