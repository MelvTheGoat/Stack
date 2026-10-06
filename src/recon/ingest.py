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
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recon import audit
from recon.config import Settings, get_settings
from recon.enums import STATUS_RANK, TransactionStatus
from recon.models import Transaction, WebhookEvent, utcnow
from recon.paystack.client import PaystackClient, VerifyUnavailableError
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
