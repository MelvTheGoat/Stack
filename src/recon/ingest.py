"""Taking a webhook in and, separately, doing the work it implies.

Two steps on purpose:

1. `receive` — check the signature, write the raw body down, answer 200. This
   has to be fast. Paystack gives the endpoint a short timeout and retries
   anything slower, roughly every 3 minutes for four attempts and then hourly
   for 72 hours. A slow handler turns one event into dozens.
2. `process` — parse it, fold it into the transaction table, and confirm it
   against a verify call. This runs in the background and may take as long as
   it likes. If it crashes, the raw body is already saved, so it can be run
   again from the stored row.

The split is also what makes the endpoint idempotent. Step 1 writes a row keyed
on the event key; a retry of the same event collides with that key, is answered
200, and never reaches step 2 a second time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recon import audit
from recon.config import Settings, get_settings
from recon.enums import STATUS_RANK, TransactionStatus
from recon.models import Transaction, WebhookEvent, utcnow
from recon.paystack.client import (
    MAX_PAGES,
    PER_PAGE,
    PaystackClient,
    VerifyUnavailableError,
    status_of,
)
from recon.paystack.events import EventError, apply_event, parse_event
from recon.paystack.signature import verify_signature


@dataclass(frozen=True, slots=True)
class Received:
    """What `receive` decided. The endpoint turns this into a status code."""

    accepted: bool
    duplicate: bool
    event_key: str | None
    reason: str = ""


def receive(
    session: Session,
    body: bytes,
    signature_header: str | None,
    settings: Settings | None = None,
) -> Received:
    """Step 1. Cheap, and it must not raise for anything a stranger can send."""
    settings = settings or get_settings()

    if not verify_signature(settings.paystack_secret_key, body, signature_header):
        # Recorded anyway, without parsing: "we never got that" is a
        # conversation you want evidence for, from both sides.
        _record_rejected(session, body)
        return Received(accepted=False, duplicate=False, event_key=None, reason="bad signature")

    try:
        payload: Any = json.loads(body)
        event = parse_event(payload if isinstance(payload, dict) else {}, body=body)
    except (json.JSONDecodeError, EventError) as exc:
        _record_rejected(session, body, note=str(exc))
        return Received(accepted=False, duplicate=False, event_key=None, reason=f"bad body: {exc}")

    if session.get(WebhookEvent, event.event_key) is not None:
        return Received(accepted=True, duplicate=True, event_key=event.event_key)

    session.add(
        WebhookEvent(
            event_key=event.event_key,
            event_type=event.event_type,
            reference=event.reference,
            signature_ok=True,
            received_at=utcnow(),
            payload_json=json.dumps(payload, sort_keys=True),
        )
    )
    try:
        session.flush()
    except IntegrityError:
        # Two retries landed at the same moment. The key did its job.
        session.rollback()
        return Received(accepted=True, duplicate=True, event_key=event.event_key)

    return Received(accepted=True, duplicate=False, event_key=event.event_key)


def _record_rejected(session: Session, body: bytes, note: str = "") -> None:
    import hashlib

    digest = hashlib.sha256(body).hexdigest()[:32]
    key = f"rejected:{digest}"
    if session.get(WebhookEvent, key) is not None:
        return
    session.add(
        WebhookEvent(
            event_key=key,
            event_type="rejected",
            signature_ok=False,
            received_at=utcnow(),
            processed_at=utcnow(),
            process_error=note or "signature did not match",
            payload_json=body.decode("utf-8", errors="replace")[:20_000],
        )
    )


def process(session: Session, event_key: str, client: PaystackClient | None = None) -> None:
    """Step 2. Runs in the background, and is safe to run again.

    Anything that goes wrong is written onto the event row and re-raised, so a
    failure is visible rather than quietly dropped.
    """
    row = session.get(WebhookEvent, event_key)
    if row is None or row.processed_at is not None:
        return

    try:
        event = parse_event(row.payload)
        if not event.is_handled:
            row.process_error = f"no handler for {event.event_type}"
            row.processed_at = utcnow()
            return

        txn = apply_event(session, event)
        if txn is not None:
            _confirm_against_paystack(session, txn, client or PaystackClient())

        audit.record(
            session,
            action="ingested",
            subject_type="transaction" if txn is not None else "dedicated_account",
            subject_id=txn.reference if txn is not None else (event.dva_account_number or "?"),
            inputs={"event_type": event.event_type, "event_key": event_key},
            evidence={
                "amount_kobo": event.amount.kobo,
                "channel": str(event.channel),
                "verified": txn.verified if txn is not None else None,
            },
        )
        row.processed_at = utcnow()
    except Exception as exc:
        row.process_error = f"{type(exc).__name__}: {exc}"
        raise


def _confirm_against_paystack(session: Session, txn: Transaction, client: PaystackClient) -> None:
    """Ask Paystack what is actually true, and believe that over the webhook.

    Three outcomes worth telling apart:
      - Paystack confirms it     -> mark verified, use its amount and fees.
      - Paystack never saw it    -> somebody sent us a payment that never was.
      - We could not ask         -> leave it unverified and move on; it stays
                                    flagged, and the next run tries again.
    """
    if client.offline:
        return

    try:
        verified = client.verify_transaction(txn.reference)
    except VerifyUnavailableError as exc:
        txn.verified = False
        audit.record(
            session,
            action="verify_unavailable",
            subject_type="transaction",
            subject_id=txn.reference,
            evidence={"error": str(exc)},
        )
        return

    if verified is None:
        txn.verified = False
        txn.status = TransactionStatus.FAILED
        audit.record(
            session,
            action="verify_unknown_reference",
            subject_type="transaction",
            subject_id=txn.reference,
            evidence={"note": "Paystack has no such reference; webhook not trusted"},
        )
        return

    claimed = txn.amount
    txn.verified = True
    if verified.amount.kobo:
        txn.amount_kobo = verified.amount.kobo
    if verified.fees.kobo:
        txn.fees_kobo = verified.fees.kobo

    # The verify call is the authority, but it is still only allowed to move the
    # payment forward, for the same out-of-order reason as everything else.
    if STATUS_RANK[verified.status] > STATUS_RANK[TransactionStatus(txn.status)]:
        txn.status = verified.status

    if claimed != verified.amount and verified.amount.kobo:
        audit.record(
            session,
            action="verify_amount_mismatch",
            subject_type="transaction",
            subject_id=txn.reference,
            evidence={
                "webhook_said_kobo": claimed.kobo,
                "paystack_said_kobo": verified.amount.kobo,
                "note": "Paystack wins",
            },
        )


# ------------------------------------------------------------- catching up


class UnreadableListingError(ValueError):
    """Paystack listed something we will not guess at, such as an amount that
    is not whole kobo. Nothing from that pull is kept."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
        self.problems = problems


@dataclass
class Pulled:
    """What a catch-up from Paystack did."""

    listed: int = 0
    added: int = 0
    updated: int = 0
    never_paid: int = 0
    complete: bool = True

    def summary(self) -> str:
        text = f"Paystack listed {self.listed}: {self.added} new, {self.updated} brought up to date"
        if self.never_paid:
            text += f", {self.never_paid} never paid (failed or abandoned, left out)"
        text += "."
        if not self.complete:
            text += (
                f" Paystack had more than {MAX_PAGES * PER_PAGE:,} in that range, so this"
                " stopped short. Start from a later date to get them all."
            )
        return text


def pull_from_paystack(
    session: Session, since: date, client: PaystackClient | None = None, *, who: str = "system"
) -> Pulled:
    """Fold every transaction Paystack lists since a date into the books.

    Through the same `apply_event` as a webhook, so the same rules hold: keyed
    on the reference, so pulling twice adds nothing, and a payment only ever
    moves forward. A refund that came in by webhook is not undone by a list
    that still says "success".

    Each one is marked verified. A list from Paystack, fetched with our own
    key, is the same answer a verify call gives, for many payments at once.

    A checkout that failed or was abandoned is left out unless it is already
    in the books. Those are most of a busy list and none of the money.

    Anything unreadable stops the whole pull, after every row has been looked
    at so all the problems are reported at once. The caller's session then
    rolls back, and nothing from that list is kept.
    """
    listing = (client or PaystackClient()).list_transactions(since)
    pulled = Pulled(listed=len(listing.rows), complete=listing.complete)
    problems: list[str] = []

    for data in listing.rows:
        try:
            event = parse_event({"event": "charge.success", "data": data})
        except EventError as exc:
            problems.append(f"{data.get('reference') or data.get('id') or '?'}: {exc}")
            continue
        status = status_of(str(data.get("status") or ""))
        event = replace(event, event_type=LISTED_EVENT, status=status)
        if event.reference is None:
            problems.append(f"transaction {data.get('id')} has no reference")
            continue

        existing = session.get(Transaction, event.reference)
        if existing is None and status is TransactionStatus.FAILED:
            pulled.never_paid += 1
            continue

        before = None if existing is None else _snapshot(existing)
        txn = apply_event(session, event)
        if txn is None:
            continue
        txn.verified = True
        if before is None:
            pulled.added += 1
        elif _snapshot(txn) != before:
            pulled.updated += 1

    if problems:
        raise UnreadableListingError(problems)

    audit.record(
        session,
        action="pulled_from_paystack",
        subject_type="upload",
        subject_id="paystack",
        actor=who,
        inputs={"since": since.isoformat()},
        evidence={
            "listed": pulled.listed,
            "added": pulled.added,
            "updated": pulled.updated,
            "never_paid": pulled.never_paid,
            "complete": pulled.complete,
        },
    )
    return pulled


#: The event type a listed transaction is folded in under. Not a real Paystack
#: event, and never stored as one; it only marks where the row came from.
LISTED_EVENT = "transaction.listed"


def _snapshot(txn: Transaction) -> tuple[Any, ...]:
    return (str(txn.status), txn.amount_kobo, txn.fees_kobo, txn.refunded_kobo, txn.verified)
