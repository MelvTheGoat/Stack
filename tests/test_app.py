"""The endpoint itself, driven over HTTP the way Paystack drives it."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from recon import db as db_module
from recon.app import app
from recon.config import Settings, get_settings
from recon.models import Transaction, WebhookEvent
from recon.money import Money
from recon.paystack.signature import SIGNATURE_HEADER
from tests import factories

KEY = "sk_test_0123456789abcdef"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A real app against a real (temporary) database file.

    A file rather than :memory: because the background task opens its own
    session, and an in-memory SQLite database is not shared between them.
    """
    url = f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
    settings = Settings(PAYSTACK_SECRET_KEY=KEY, RECON_DATABASE_URL=url, RECON_OFFLINE=True)

    db_module.reset_engine()
    app.dependency_overrides[get_settings] = lambda: settings
    get_settings.cache_clear()

    import os

    os.environ["RECON_DATABASE_URL"] = url
    os.environ["PAYSTACK_SECRET_KEY"] = KEY
    os.environ["RECON_OFFLINE"] = "1"

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    get_settings.cache_clear()
    db_module.reset_engine()


def post(client: TestClient, body: dict[str, Any], *, sign: bool = True) -> Any:
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if sign:
        headers[SIGNATURE_HEADER] = hmac.new(KEY.encode(), raw, hashlib.sha512).hexdigest()
    return client.post("/webhooks/paystack", content=raw, headers=headers)


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_a_signed_webhook_gets_a_200_and_becomes_a_transaction(client: TestClient) -> None:
    response = post(client, factories.charge_success())
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    with db_module.session_scope() as session:
        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert txn.amount == Money(125050)


def test_an_unsigned_webhook_gets_401(client: TestClient) -> None:
    response = post(client, factories.charge_success(), sign=False)
    assert response.status_code == 401
    assert response.json()["reason"] == "bad signature"


def test_a_bad_signature_gets_401_and_writes_nothing(client: TestClient) -> None:
    raw = json.dumps(factories.charge_success()).encode()
    response = client.post("/webhooks/paystack", content=raw, headers={SIGNATURE_HEADER: "0" * 128})
    assert response.status_code == 401
    with db_module.session_scope() as session:
        assert session.query(Transaction).count() == 0


def test_a_retry_gets_200_and_is_reported_as_a_duplicate(client: TestClient) -> None:
    body = factories.charge_success()
    first = post(client, body)
    second = post(client, body)

    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    with db_module.session_scope() as session:
        assert session.query(WebhookEvent).count() == 1
        assert session.query(Transaction).count() == 1


def test_a_dedicated_account_assignment_is_accepted(client: TestClient) -> None:
    response = post(client, factories.dedicated_account_assigned())
    assert response.status_code == 200

    from recon.models import DedicatedAccount

    with db_module.session_scope() as session:
        assert session.get(DedicatedAccount, "9912345678") is not None


def test_an_event_we_have_no_handler_for_is_still_acknowledged(client: TestClient) -> None:
    """A 200 keeps Paystack from retrying something we are never going to act on."""
    response = post(client, {"event": "customer.identification.failed", "data": {"id": 4}})
    assert response.status_code == 200

    with db_module.session_scope() as session:
        row = session.query(WebhookEvent).one()
        assert row.process_error is not None
        assert "no handler" in row.process_error


def test_the_whole_out_of_order_story_over_http(client: TestClient) -> None:
    """Refund first, charge second. The books must still say refunded."""
    post(client, factories.refund_processed("ref_ooo", 60_000))
    post(client, factories.charge_success("ref_ooo", 60_000))

    with db_module.session_scope() as session:
        txn = session.get(Transaction, "ref_ooo")
        assert txn is not None
        assert str(txn.status) == "refunded"
        assert txn.refunded_kobo == 60_000
