"""Database tables.

Two things to notice.

1. Every money column is named `*_kobo` and is a plain integer. There is no
   float column anywhere in this file, and `amount` properties hand back a
   `Money` so callers cannot accidentally do arithmetic on the raw integer.
2. `AuditRecord` is append-only. Nothing in the codebase updates or deletes a
   row in it. If you ever want to know why the books said what they said on a
   given evening, that table is the answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from recon.enums import (
    Channel,
    DecisionStatus,
    Layer,
    OrderStatus,
    RejectReason,
    TransactionStatus,
)
from recon.money import Money


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


def _json_column(**kwargs: Any) -> Mapped[str]:
    return mapped_column(Text, **kwargs)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    dedicated_accounts: Mapped[list[DedicatedAccount]] = relationship(back_populates="customer")
    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class DedicatedAccount(Base):
    """A bank account number that belongs to exactly one customer.

    This is the cheat code of the whole system. If money lands in Ada's
    dedicated account, it is Ada's money. No name matching required.
    """

    __tablename__ = "dedicated_accounts"

    account_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    bank: Mapped[str] = mapped_column(String(100))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    customer: Mapped[Customer] = relationship(back_populates="dedicated_accounts")


class Order(Base):
    """One thing the business is owed money for."""

    __tablename__ = "orders"

    reference: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_kobo: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[OrderStatus] = mapped_column(String(16), default=OrderStatus.OPEN)
    description: Mapped[str] = mapped_column(String(255), default="")

    customer: Mapped[Customer] = relationship(back_populates="orders")

    @property
    def amount(self) -> Money:
        return Money(self.amount_kobo)


class Transaction(Base):
    """One movement of money we were told about.

    `reference` is Paystack's reference, and it is the idempotency key for the
    whole pipeline. Cash rows get a made-up reference with a `cash_` prefix so
    the same key rule holds for them too.
    """

    __tablename__ = "transactions"

    reference: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel: Mapped[Channel] = mapped_column(String(20), index=True)
    status: Mapped[TransactionStatus] = mapped_column(
        String(16), default=TransactionStatus.PENDING, index=True
    )
    amount_kobo: Mapped[int] = mapped_column(Integer)
    fees_kobo: Mapped[int] = mapped_column(Integer, default=0)
    refunded_kobo: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")

    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    #: Whatever the payer wrote or the bank passed along. Freeform, often a mess.
    narration: Mapped[str] = mapped_column(String(500), default="")
    payer_name: Mapped[str] = mapped_column(String(255), default="")
    #: Set when the payer typed our order reference into the payment. Gold dust.
    stated_reference: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    dva_account_number: Mapped[str | None] = mapped_column(String(20), default=None, index=True)

    settlement_id: Mapped[str | None] = mapped_column(
        ForeignKey("settlements.id"), default=None, index=True
    )
    #: True once a verify call confirmed this, rather than the webhook alone.
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[str] = _json_column(default="{}")

    @property
    def amount(self) -> Money:
        return Money(self.amount_kobo)

    @property
    def fees(self) -> Money:
        return Money(self.fees_kobo)

    @property
    def net(self) -> Money:
        """What actually reaches the bank: gross minus fees minus refunds."""
        return Money(self.amount_kobo - self.fees_kobo - self.refunded_kobo)


class Settlement(Base):
    """A batch Paystack pays into the bank account, net of fees, usually T+1."""

    __tablename__ = "settlements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    gross_kobo: Mapped[int] = mapped_column(Integer)
    fees_kobo: Mapped[int] = mapped_column(Integer)
    net_kobo: Mapped[int] = mapped_column(Integer)

    @property
    def gross(self) -> Money:
        return Money(self.gross_kobo)

    @property
    def fees(self) -> Money:
        return Money(self.fees_kobo)

    @property
    def net(self) -> Money:
        return Money(self.net_kobo)


class WebhookEvent(Base):
    """Raw record of every webhook we were sent.

    Kept forever, including the ones whose signature failed, because "we never
    got that" is a conversation you want evidence for. The unique constraint on
    `event_key` is what makes the endpoint idempotent: Paystack retries roughly
    every 3 minutes four times, then hourly for 72 hours, so the same event will
    arrive again and must not be processed twice.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_received", "received_at"),)

    event_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    reference: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    signature_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    process_error: Mapped[str | None] = mapped_column(Text, default=None)
    payload_json: Mapped[str] = _json_column(default="{}")

    @property
    def payload(self) -> dict[str, Any]:
        parsed: Any = json.loads(self.payload_json)
        return parsed if isinstance(parsed, dict) else {}


class MatchDecision(Base):
    """What we think a transaction was for, and who decided that."""

    __tablename__ = "match_decisions"
    __table_args__ = (UniqueConstraint("transaction_reference", name="uq_decision_per_txn"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_reference: Mapped[str] = mapped_column(
        ForeignKey("transactions.reference"), index=True
    )
    #: JSON list of order references. Usually one; sometimes a transfer covers three.
    order_references_json: Mapped[str] = _json_column(default="[]")
    layer: Mapped[Layer] = mapped_column(String(24), index=True)
    #: Calibrated probability the match is right, 0.0 to 1.0. 1.0 for exact matches.
    confidence: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[DecisionStatus] = mapped_column(String(16), index=True)
    #: Money we would get wrong if this decision is wrong. Ranks the review queue.
    money_at_risk_kobo: Mapped[int] = mapped_column(Integer, default=0)
    evidence_json: Mapped[str] = _json_column(default="{}")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    reject_reason: Mapped[RejectReason | None] = mapped_column(String(24), default=None)

    @property
    def order_references(self) -> list[str]:
        parsed: Any = json.loads(self.order_references_json)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []

    @property
    def evidence(self) -> dict[str, Any]:
        parsed: Any = json.loads(self.evidence_json)
        return parsed if isinstance(parsed, dict) else {}

    @property
    def money_at_risk(self) -> Money:
        return Money(self.money_at_risk_kobo)


class AuditRecord(Base):
    """Append-only. Insert only. Never update, never delete.

    Every resolution writes one of these: what went in, which layer decided,
    what score it gave, what evidence it used, when, and for a human decision,
    who.
    """

    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_subject", "subject_type", "subject_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(128))
    layer: Mapped[Layer | None] = mapped_column(String(24), default=None)
    confidence: Mapped[float | None] = mapped_column(default=None)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    inputs_json: Mapped[str] = _json_column(default="{}")
    evidence_json: Mapped[str] = _json_column(default="{}")
