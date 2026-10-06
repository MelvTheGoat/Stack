"""Ingest: signature, idempotency, and believing the verify call over the webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from recon import ingest
from recon.config import Settings
from recon.enums import TransactionStatus
from recon.models import AuditRecord, Transaction, WebhookEvent
from recon.money import Money
from recon.paystack.client import (
    KeyRefusedError,
    PaystackClient,
    PaystackUnavailableError,
    VerifyUnavailableError,
)
from tests import factories

KEY = "sk_test_0123456789abcdef"
SETTINGS = Settings(PAYSTACK_SECRET_KEY=KEY, RECON_OFFLINE=True)


def signed(body: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(KEY.encode(), raw, hashlib.sha512).hexdigest()


def fake_paystack(handler: Any, offline: bool = False) -> PaystackClient:
    settings = Settings(PAYSTACK_SECRET_KEY=KEY, RECON_OFFLINE=offline)
    transport = httpx.MockTransport(handler)
    return PaystackClient(
        settings, httpx.Client(transport=transport, base_url="https://api.paystack.co")
    )


class TestSignature:
    def test_a_signed_event_is_accepted(self, session: Session) -> None:
        body, signature = signed(factories.charge_success())
        result = ingest.receive(session, body, signature, SETTINGS)
        assert result.accepted and not result.duplicate

    def test_an_unsigned_event_is_rejected(self, session: Session) -> None:
        body, _ = signed(factories.charge_success())
        result = ingest.receive(session, body, None, SETTINGS)
        assert not result.accepted
        assert result.reason == "bad signature"

    def test_a_tampered_amount_is_rejected(self, session: Session) -> None:
        body, signature = signed(factories.charge_success(amount_kobo=1000))
        tampered = body.replace(b'"amount": 1000', b'"amount": 9999')
        result = ingest.receive(session, tampered, signature, SETTINGS)
        assert not result.accepted

    def test_a_rejected_event_is_still_written_down(self, session: Session) -> None:
        body, _ = signed(factories.charge_success())
        ingest.receive(session, body, "deadbeef", SETTINGS)
        session.commit()
        rows = session.query(WebhookEvent).filter(WebhookEvent.signature_ok.is_(False)).all()
        assert len(rows) == 1
        assert rows[0].process_error

    def test_a_forged_event_never_becomes_a_transaction(self, session: Session) -> None:
        body, _ = signed(factories.charge_success())
        ingest.receive(session, body, "deadbeef", SETTINGS)
        session.commit()
        assert session.query(Transaction).count() == 0

    def test_junk_that_is_correctly_signed_is_still_refused(self, session: Session) -> None:
        raw = b"this is not json"
        signature = hmac.new(KEY.encode(), raw, hashlib.sha512).hexdigest()
        result = ingest.receive(session, raw, signature, SETTINGS)
        assert not result.accepted
        assert "bad body" in result.reason


class TestIdempotency:
    def test_the_same_event_twice_is_stored_once(self, session: Session) -> None:
        body, signature = signed(factories.charge_success())
        first = ingest.receive(session, body, signature, SETTINGS)
        session.commit()
        second = ingest.receive(session, body, signature, SETTINGS)
        session.commit()

        assert first.accepted and not first.duplicate
        assert second.accepted and second.duplicate
        assert session.query(WebhookEvent).count() == 1

    def test_paystacks_whole_retry_schedule_produces_one_transaction(
        self, session: Session
    ) -> None:
        """Four quick retries then hourly for 72 hours. Same event, over and over."""
        body, signature = signed(factories.charge_success())
        keys = []
        for _ in range(76):
            result = ingest.receive(session, body, signature, SETTINGS)
            session.commit()
            if result.event_key:
                keys.append(result.event_key)
                ingest.process(session, result.event_key, fake_paystack(None, offline=True))
                session.commit()

        assert len(set(keys)) == 1
        assert session.query(WebhookEvent).count() == 1
        assert session.query(Transaction).count() == 1
        txn = session.query(Transaction).one()
        assert txn.amount == Money(125050), "an amount must never be applied twice"

    def test_processing_twice_does_nothing_the_second_time(self, session: Session) -> None:
        body, signature = signed(factories.charge_success())
        result = ingest.receive(session, body, signature, SETTINGS)
        session.commit()
        assert result.event_key

        ingest.process(session, result.event_key, fake_paystack(None, offline=True))
        session.commit()
        audits = session.query(AuditRecord).count()

        ingest.process(session, result.event_key, fake_paystack(None, offline=True))
        session.commit()
        assert session.query(AuditRecord).count() == audits

    def test_a_refund_and_its_charge_are_different_events(self, session: Session) -> None:
        for body in (factories.charge_success("ref_z"), factories.refund_processed("ref_z")):
            raw, signature = signed(body)
            ingest.receive(session, raw, signature, SETTINGS)
        session.commit()
        assert session.query(WebhookEvent).count() == 2


class TestVerifyIsTheAuthority:
    def paystack_says(self, **data: Any) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": True, "data": data})

        return handler

    def ingest_one(self, session: Session, body: dict[str, Any], client: PaystackClient) -> None:
        raw, signature = signed(body)
        result = ingest.receive(session, raw, signature, SETTINGS)
        assert result.event_key
        ingest.process(session, result.event_key, client)
        session.commit()

    def test_paystacks_amount_wins_over_the_webhooks(self, session: Session) -> None:
        client = fake_paystack(
            self.paystack_says(reference="ref_card_1", status="success", amount=99_900, fees=1_598)
        )
        self.ingest_one(session, factories.charge_success(amount_kobo=125050), client)

        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert txn.verified
        assert txn.amount == Money(99_900), "the webhook said 125050; Paystack is the authority"

        actions = [a.action for a in session.query(AuditRecord).all()]
        assert "verify_amount_mismatch" in actions

    def test_a_reference_paystack_never_heard_of_is_not_banked(self, session: Session) -> None:
        def not_found(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"status": False})

        self.ingest_one(session, factories.charge_success(), fake_paystack(not_found))

        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert not txn.verified
        assert txn.status is TransactionStatus.FAILED
        actions = [a.action for a in session.query(AuditRecord).all()]
        assert "verify_unknown_reference" in actions

    def test_paystack_being_down_leaves_it_unverified_not_wrong(self, session: Session) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        self.ingest_one(session, factories.charge_success(), fake_paystack(boom))

        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert not txn.verified
        assert txn.status is TransactionStatus.SUCCESS, "we still recorded what we were told"
        actions = [a.action for a in session.query(AuditRecord).all()]
        assert "verify_unavailable" in actions

    def test_verify_cannot_walk_a_refund_back_to_a_success(self, session: Session) -> None:
        """Verify is the authority on amounts, but the forward-only rule still holds."""
        client = fake_paystack(
            self.paystack_says(reference="ref_card_1", status="success", amount=125050, fees=1976)
        )
        self.ingest_one(session, factories.refund_processed("ref_card_1"), client)

        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert txn.status is TransactionStatus.REFUNDED

    def test_a_500_from_paystack_is_unavailable_not_unknown(self, session: Session) -> None:
        def flaky(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = fake_paystack(flaky)
        with pytest.raises(VerifyUnavailableError):
            client.verify_transaction("ref_card_1")


def listed(
    reference: str, kobo: int = 500_000, status: str = "success", **extra: Any
) -> dict[str, Any]:
    """One transaction as Paystack's list endpoint returns it."""
    data = factories.charge_success(reference, kobo, status=status)["data"]
    return {**data, **extra}


def pages(*batches: list[dict[str, Any]]) -> Any:
    """A fake Paystack that serves `batches` as pages 1, 2, ... in order."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        rows = batches[page - 1] if page <= len(batches) else []
        meta = {"total": sum(map(len, batches)), "page": page, "pageCount": len(batches)}
        return httpx.Response(200, json={"status": True, "data": rows, "meta": meta})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


SINCE = date(2024, 5, 1)


class TestCatchingUpFromPaystack:
    def test_every_page_is_read_and_every_payment_is_verified(self, session: Session) -> None:
        handler = pages([listed("ref_1"), listed("ref_2")], [listed("ref_3")])
        pulled = ingest.pull_from_paystack(session, SINCE, fake_paystack(handler))

        assert (pulled.listed, pulled.added, pulled.complete) == (3, 3, True)
        rows = session.query(Transaction).order_by(Transaction.reference).all()
        assert [row.reference for row in rows] == ["ref_1", "ref_2", "ref_3"]
        assert all(row.verified for row in rows)
        assert all(row.status is TransactionStatus.SUCCESS for row in rows)

        first = handler.seen[0].url.params
        assert (first["from"], first["perPage"], first["page"]) == ("2024-05-01", "100", "1")

    def test_pulling_twice_adds_nothing(self, session: Session) -> None:
        handler = pages([listed("ref_1"), listed("ref_2")])
        ingest.pull_from_paystack(session, SINCE, fake_paystack(handler))
        again = ingest.pull_from_paystack(session, SINCE, fake_paystack(handler))
        assert (again.added, again.updated) == (0, 0)
        assert session.query(Transaction).count() == 2

    def test_an_abandoned_checkout_is_left_out(self, session: Session) -> None:
        handler = pages([listed("ref_1"), listed("ref_2", status="abandoned")])
        pulled = ingest.pull_from_paystack(session, SINCE, fake_paystack(handler))
        assert (pulled.added, pulled.never_paid) == (1, 1)
        assert session.get(Transaction, "ref_2") is None

    def test_a_list_cannot_walk_a_refund_back_to_a_success(self, session: Session) -> None:
        """The refund came in by webhook. Paystack's list can lag behind it."""
        raw, sig = signed(factories.refund_processed("ref_1", 500_000))
        key = ingest.receive(session, raw, sig, Settings(PAYSTACK_SECRET_KEY=KEY)).event_key
        assert key is not None
        ingest.process(session, key, fake_paystack(lambda r: httpx.Response(404), offline=True))

        ingest.pull_from_paystack(session, SINCE, fake_paystack(pages([listed("ref_1")])))
        txn = session.get(Transaction, "ref_1")
        assert txn is not None and txn.status is TransactionStatus.REFUNDED

    def test_a_payment_only_a_webhook_vouched_for_becomes_verified(self, session: Session) -> None:
        raw, sig = signed(factories.charge_success("ref_1", 500_000))
        key = ingest.receive(session, raw, sig, Settings(PAYSTACK_SECRET_KEY=KEY)).event_key
        assert key is not None
        ingest.process(session, key, fake_paystack(lambda r: httpx.Response(404), offline=True))
        assert not session.get(Transaction, "ref_1").verified  # type: ignore[union-attr]

        pulled = ingest.pull_from_paystack(session, SINCE, fake_paystack(pages([listed("ref_1")])))
        assert pulled.updated == 1
        assert session.get(Transaction, "ref_1").verified  # type: ignore[union-attr]

    def test_an_amount_that_is_not_whole_kobo_stops_the_pull(self, session: Session) -> None:
        handler = pages([listed("ref_1"), listed("ref_2", amount=5000.5)])
        with pytest.raises(ingest.UnreadableListingError, match="ref_2"):
            ingest.pull_from_paystack(session, SINCE, fake_paystack(handler))

    def test_a_cap_that_is_hit_is_reported(self) -> None:
        handler = pages([listed("ref_1")], [listed("ref_2")], [listed("ref_3")])
        listing = fake_paystack(handler).list_transactions(SINCE, max_pages=2)
        assert len(listing.rows) == 2
        assert not listing.complete

    def test_a_wrong_key_is_said_plainly(self) -> None:
        client = fake_paystack(lambda r: httpx.Response(401, json={"status": False}))
        with pytest.raises(KeyRefusedError):
            client.list_transactions(SINCE)

    def test_offline_it_does_not_call_out(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("called Paystack while offline")

        with pytest.raises(PaystackUnavailableError, match="OFFLINE"):
            fake_paystack(explode, offline=True).list_transactions(SINCE)

    def test_every_pull_is_written_to_the_audit_log(self, session: Session) -> None:
        ingest.pull_from_paystack(
            session, SINCE, fake_paystack(pages([listed("ref_1")])), who="ada"
        )
        entry = (
            session.query(AuditRecord).filter(AuditRecord.action == "pulled_from_paystack").one()
        )
        assert entry.actor == "ada"
