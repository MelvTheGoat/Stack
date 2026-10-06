"""Ingest: signature, idempotency, and believing the verify call over the webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from recon import ingest
from recon.config import Settings
from recon.enums import TransactionStatus
from recon.models import AuditRecord, Transaction, WebhookEvent
from recon.money import Money
from recon.paystack.client import PaystackClient, VerifyUnavailableError
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
