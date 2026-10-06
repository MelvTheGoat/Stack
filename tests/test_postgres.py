"""The same books, kept in Postgres, which is where a real instance keeps them.

SQLite forgives things Postgres does not: a string longer than its column, a
foreign key to nothing, a timestamp with no zone. These run the database paths
against a real Postgres, and are skipped unless one is given:

    RECON_TEST_POSTGRES_URL=postgresql://user@localhost:5432/reckon_test pytest
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from recon import imports, state
from recon.config import get_settings
from recon.db import get_engine, reset_engine, session_scope
from recon.enums import Layer, RejectReason
from recon.models import Base, MatchDecision
from recon.review import queue as review_queue
from tests.test_books import load_corpus

URL = os.environ.get("RECON_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RECON_TEST_POSTGRES_URL is not set")


@pytest.fixture(autouse=True)
def postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("RECON_DATABASE_URL", raising=False)
    monkeypatch.delenv("RECON_DEMO", raising=False)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_postgres")
    monkeypatch.setenv("RECON_OFFLINE", "1")
    get_settings.cache_clear()
    reset_engine()
    state.reset()
    engine = get_engine()
    assert engine.dialect.name == "postgresql"
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield

    state.reset()
    reset_engine()
    get_settings.cache_clear()


def test_the_corpus_gets_the_same_certain_answers_from_postgres() -> None:
    demo = state.load()
    with session_scope() as session:
        load_corpus(session)
    own = state.from_database()

    certain = {
        m.transaction_reference: m.order_references
        for m in demo.matches
        if m.resolved and m.layer is not Layer.PROBABILISTIC
    }
    ours = {m.transaction_reference: m.order_references for m in own.matches}
    assert all(ours[reference] == orders for reference, orders in certain.items())
    assert len(own.queue.items) == len(demo.matches) - len(certain)


def test_an_upload_and_a_decision_round_trip() -> None:
    with session_scope() as session:
        assert imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode()).ok
        assert imports.import_payments(
            session,
            b"date,amount,narration\n2024-05-19,40000,TRANSFER FROM A STRANGER\n",
        ).ok
    state.changed()
    (item,) = state.workspace().queue.items

    with session_scope() as session:
        review_queue.reject(session, item, RejectReason.NOT_A_PAYMENT, who="ada")
    state.changed()

    assert state.workspace().queue.items == []
    with session_scope() as session:
        assert session.query(MatchDecision).one().decided_by == "ada"


def test_a_webhook_lands_in_postgres_and_on_the_pages() -> None:
    import hashlib
    import hmac
    import json

    from fastapi.testclient import TestClient

    from recon.app import app
    from recon.paystack.signature import SIGNATURE_HEADER
    from tests import factories

    raw = json.dumps(factories.charge_success("ref_pg")).encode()
    signature = hmac.new(b"sk_test_postgres", raw, hashlib.sha512).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/paystack", content=raw, headers={SIGNATURE_HEADER: signature}
        )
    assert response.json()["status"] == "accepted"
    assert [txn.reference for txn in state.workspace().transactions] == ["ref_pg"]


def test_an_account_signs_up_is_let_in_and_logs_in() -> None:
    from recon import accounts
    from recon.config import Settings

    with session_scope() as session:
        boss = accounts.ensure_admin(
            session,
            Settings(
                PAYSTACK_SECRET_KEY="sk_test_postgres",
                RECON_ADMIN_EMAIL="boss@example.com",
                RECON_PASSWORD="a long enough password",
            ),
        )
        assert boss is not None
        ada = accounts.sign_up(
            session, name="Ada Okonkwo", email="ada@example.com", password="ada's password!"
        )
        accounts.change(session, boss, ada.id, "approve")

    with session_scope() as session:
        _, token = accounts.log_in(session, email="ada@example.com", password="ada's password!")
    with session_scope() as session:
        user = accounts.user_for(session, token)
        assert user is not None and user.email == "ada@example.com"
