"""Turning a Paystack webhook body into one flat shape the rest of the app uses.

Paystack's six event types we care about all say "money moved" in slightly
different words. This module flattens them, and then applies them to the
transaction table under one rule:

    **A payment only ever moves forward.**

That rule exists because webhook delivery order is not guaranteed. Paystack
retries roughly every 3 minutes for four attempts and then hourly for 72 hours,
so `refund.processed` really can land before the `charge.success` it refunds.
If we just wrote whatever the latest message said, a late success would quietly
un-refund a refunded payment. Instead every status has a rank and an event is
ignored unless it moves the payment up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from recon.enums import STATUS_RANK, Channel, TransactionStatus
from recon.models import Customer, DedicatedAccount, Transaction, utcnow
from recon.money import Money

#: The events this service knows what to do with. Anything else is stored and
#: acknowledged, because a 200 is cheaper for both sides than a retry storm.
HANDLED_EVENTS: frozenset[str] = frozenset(
    {
        "charge.success",
        "transfer.success",
        "paymentrequest.success",
        "paymentrequest.pending",
        "refund.processed",
        "dedicatedaccount.assign.success",
    }
)

_CHANNEL_MAP: dict[str, Channel] = {
    "card": Channel.CARD,
    "dedicated_nuban": Channel.DVA,
    "dedicated_account": Channel.DVA,
    "bank_transfer": Channel.BANK_TRANSFER,
    "bank": Channel.BANK_TRANSFER,
    "transfer": Channel.BANK_TRANSFER,
    "cash": Channel.CASH,
}


class EventError(ValueError):
    """The webhook body was not shaped like a Paystack event."""


@dataclass(frozen=True, slots=True)
class NormalisedEvent:
    """One Paystack event, flattened."""

    event_type: str
    event_key: str
    """Idempotency key. The same event delivered twice produces the same key."""

    occurred_at: datetime
    reference: str | None = None
    channel: Channel = Channel.UNKNOWN
    amount: Money = field(default_factory=Money.zero)
    fees: Money = field(default_factory=Money.zero)
    status: TransactionStatus | None = None
    payer_name: str = ""
    narration: str = ""
    stated_reference: str | None = None
    dva_account_number: str | None = None
    customer_id: str | None = None
    customer_name: str = ""
    bank: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_handled(self) -> bool:
        return self.event_type in HANDLED_EVENTS


# ------------------------------------------------------------------ parsing


def parse_event(payload: dict[str, Any], *, body: bytes | None = None) -> NormalisedEvent:
    """Flatten a webhook body. Raises `EventError` if it is not an event at all."""
    if not isinstance(payload, dict) or "event" not in payload:
        raise EventError("webhook body has no 'event' field")

    event_type = str(payload["event"])
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}

    reference = _first_str(data, "reference", "transaction_reference", "offline_reference")
    amount = _money_from(data, "amount")
    fees = _money_from(data, "fees")
    occurred_at = _timestamp(data, payload)

    return NormalisedEvent(
        event_type=event_type,
        event_key=build_event_key(event_type, data, body=body),
        occurred_at=occurred_at,
        reference=reference,
        channel=_channel(event_type, data),
        amount=amount,
        fees=fees,
        status=_status_for(event_type, data),
        payer_name=_payer_name(data),
        narration=_narration(data),
        stated_reference=_stated_reference(data),
        dva_account_number=_dva_account_number(data),
        customer_id=_customer_id(data),
        customer_name=_customer_name(data),
        bank=_bank(data),
        raw=payload,
    )


def build_event_key(event_type: str, data: dict[str, Any], *, body: bytes | None = None) -> str:
    """A stable key for one delivery of one event.

    Paystack does not put a dedicated event id in the body, so we build one from
    the event type plus the most specific identifier the payload carries. A
    retry of the same event carries the same ids, so it collapses onto the same
    key and gets skipped. Two genuinely different events never collide, because
    the type is part of the key.

    Last resort is a hash of the raw body, which is still correct for retries
    (Paystack resends identical bytes) and only risks conflating two events that
    were byte-identical, which would mean they were the same event anyway.
    """
    for key in ("id", "reference", "account_number", "domain"):
        value = data.get(key)
        if value not in (None, ""):
            return f"{event_type}:{value}"
    digest = hashlib.sha256(body or json.dumps(data, sort_keys=True).encode()).hexdigest()
    return f"{event_type}:body:{digest[:32]}"


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _money_from(data: dict[str, Any], key: str) -> Money:
    """Paystack sends NGN amounts as integer kobo. Anything else is a bug we shout about."""
    value = data.get(key, 0)
    if value in (None, ""):
        return Money.zero()
    if isinstance(value, bool) or not isinstance(value, int):
        # A float here would mean Paystack changed its contract or someone
        # pre-processed the payload. Either way, do not guess at the amount.
        raise EventError(f"{key!r} should be whole kobo, got {type(value).__name__}: {value!r}")
    return Money(value)


def _timestamp(data: dict[str, Any], payload: dict[str, Any]) -> datetime:
    for key in ("paid_at", "paidAt", "created_at", "createdAt", "transaction_date", "updated_at"):
        value = data.get(key) or payload.get(key)
        if isinstance(value, str) and value.strip():
            parsed = _parse_iso(value)
            if parsed is not None:
                return parsed
    return utcnow()


def _parse_iso(value: str) -> datetime | None:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _channel(event_type: str, data: dict[str, Any]) -> Channel:
    raw = str(data.get("channel") or "").lower()
    if raw in _CHANNEL_MAP:
        return _CHANNEL_MAP[raw]
    if event_type.startswith("dedicatedaccount") or _dva_account_number(data):
        return Channel.DVA
    if event_type.startswith("transfer"):
        return Channel.BANK_TRANSFER
    return Channel.UNKNOWN


def _status_for(event_type: str, data: dict[str, Any]) -> TransactionStatus | None:
    if event_type == "refund.processed":
        return TransactionStatus.REFUNDED
    if event_type == "charge.dispute.remind" or event_type.endswith(".reversed"):
        return TransactionStatus.REVERSED
    if event_type == "paymentrequest.pending":
        return TransactionStatus.PENDING
    if event_type == "dedicatedaccount.assign.success":
        return None
    if event_type.endswith(".success"):
        # Trust the payload's own status word when it disagrees with the topic.
        stated = str(data.get("status") or "success").lower()
        if stated in ("reversed", "reversal"):
            return TransactionStatus.REVERSED
        if stated in ("failed", "abandoned"):
            return TransactionStatus.FAILED
        return TransactionStatus.SUCCESS
    if event_type.endswith(".failed"):
        return TransactionStatus.FAILED
    return None


def _authorization(data: dict[str, Any]) -> dict[str, Any]:
    auth = data.get("authorization")
    return auth if isinstance(auth, dict) else {}


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    meta = data.get("metadata")
    if isinstance(meta, str):
        try:
            parsed: Any = json.loads(meta)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _payer_name(data: dict[str, Any]) -> str:
    auth = _authorization(data)
    return (
        _first_str(auth, "sender_name", "account_name")
        or _first_str(_metadata(data), "payer_name", "sender_name")
        or _customer_name(data)
    )


def _narration(data: dict[str, Any]) -> str:
    """Whatever free text came with the money. For a main-account transfer this
    is all we get, and it is where most of the matching pain lives."""
    auth = _authorization(data)
    return (
        _first_str(data, "narration", "description", "reason")
        or _first_str(auth, "narration", "sender_name")
        or _first_str(_metadata(data), "narration")
        or ""
    )


def _stated_reference(data: dict[str, Any]) -> str | None:
    """An order reference the payer or the checkout explicitly supplied.

    Only trusted when it was passed as structured data. Anything dug out of a
    narration string is a guess, and guesses belong in the matcher, not here.
    """
    meta = _metadata(data)
    direct = _first_str(meta, "order_reference", "invoice_reference", "order_id")
    if direct:
        return direct
    fields = meta.get("custom_fields")
    if isinstance(fields, list):
        for entry in fields:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("variable_name") or entry.get("display_name") or "").lower()
            if "order" in name or "invoice" in name:
                value = entry.get("value")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _dva_account_number(data: dict[str, Any]) -> str | None:
    auth = _authorization(data)
    account = data.get("dedicated_account")
    account = account if isinstance(account, dict) else {}
    return (
        _first_str(account, "account_number")
        or _first_str(auth, "receiver_bank_account_number", "account_number")
        or _first_str(data, "account_number")
    )


def _customer_id(data: dict[str, Any]) -> str | None:
    customer = data.get("customer")
    if isinstance(customer, dict):
        return _first_str(customer, "customer_code", "id") or None
    return None


def _customer_name(data: dict[str, Any]) -> str:
    customer = data.get("customer")
    if not isinstance(customer, dict):
        return ""
    parts = [str(customer.get("first_name") or ""), str(customer.get("last_name") or "")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or str(customer.get("email") or "")


def _bank(data: dict[str, Any]) -> str:
    account = data.get("dedicated_account")
    if isinstance(account, dict):
        bank = account.get("bank")
        if isinstance(bank, dict):
            return str(bank.get("name") or "")
        if isinstance(bank, str):
            return bank
    return _first_str(_authorization(data), "bank", "sender_bank") or ""


# --------------------------------------------------------------- applying


def apply_event(session: Session, event: NormalisedEvent) -> Transaction | None:
    """Fold one event into the transaction table. Safe to call out of order.

    Returns the transaction it touched, or None for events that are not about a
    transaction (a dedicated-account assignment, say).
    """
    if event.event_type == "dedicatedaccount.assign.success":
        _assign_dedicated_account(session, event)
        return None

    if event.reference is None:
        raise EventError(f"{event.event_type} carried no reference, so nothing can be keyed on it")

    txn = session.get(Transaction, event.reference)
    if txn is None:
        # Column defaults only fire when the row is flushed, and this object is
        # read back before that happens, so spell every field out here.
        txn = Transaction(
            reference=event.reference,
            channel=event.channel,
            status=TransactionStatus.PENDING,
            amount_kobo=event.amount.kobo,
            fees_kobo=0,
            refunded_kobo=0,
            currency="NGN",
            paid_at=event.occurred_at,
            last_event_at=event.occurred_at,
            narration="",
            payer_name="",
            verified=False,
            raw_json="{}",
        )
        session.add(txn)

    _advance_status(txn, event)

    # Details are filled in from whichever event carried them; a later event
    # with a blank field must not wipe a value an earlier one supplied.
    if event.amount.kobo and event.event_type != "refund.processed":
        txn.amount_kobo = event.amount.kobo
    if event.fees.kobo:
        txn.fees_kobo = event.fees.kobo
    if event.event_type == "refund.processed" and event.amount.kobo:
        txn.refunded_kobo = event.amount.kobo
    if event.channel is not Channel.UNKNOWN and txn.channel is Channel.UNKNOWN:
        txn.channel = event.channel
    txn.narration = txn.narration or event.narration
    txn.payer_name = txn.payer_name or event.payer_name
    txn.stated_reference = txn.stated_reference or event.stated_reference
    txn.dva_account_number = txn.dva_account_number or event.dva_account_number
    txn.last_event_at = max(txn.last_event_at, event.occurred_at)
    txn.raw_json = json.dumps(event.raw, default=str, sort_keys=True)
    return txn


def _advance_status(txn: Transaction, event: NormalisedEvent) -> None:
    """Move the payment forward, never backward. This is the out-of-order rule."""
    if event.status is None:
        return
    current_rank = STATUS_RANK[TransactionStatus(txn.status)]
    incoming_rank = STATUS_RANK[event.status]
    if incoming_rank > current_rank:
        txn.status = event.status
        if event.status is TransactionStatus.SUCCESS:
            txn.paid_at = event.occurred_at


def _assign_dedicated_account(session: Session, event: NormalisedEvent) -> None:
    """Remember that this account number belongs to this customer.

    Worth its own event because it is the one attribution that needs no guessing
    at all: money into Ada's dedicated account is Ada's money.
    """
    if not event.dva_account_number or not event.customer_id:
        raise EventError("dedicated account assignment needs both an account number and a customer")

    if session.get(Customer, event.customer_id) is None:
        session.add(Customer(id=event.customer_id, name=event.customer_name or event.customer_id))

    existing = session.get(DedicatedAccount, event.dva_account_number)
    if existing is None:
        session.add(
            DedicatedAccount(
                account_number=event.dva_account_number,
                bank=event.bank or "unknown",
                customer_id=event.customer_id,
                assigned_at=event.occurred_at,
            )
        )
    else:
        existing.customer_id = event.customer_id
        existing.bank = existing.bank or event.bank
