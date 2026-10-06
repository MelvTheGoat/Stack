"""Who may open the pages."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from recon.config import get_settings
from recon.db import session_scope
from recon.enums import Channel, TransactionStatus
from recon.models import MatchDecision, Transaction
from recon.paystack.signature import SIGNATURE_HEADER
from tests import factories

PAGES = ("/review", "/report", "/api/queue", "/api/report", "/api/labels")


@pytest.fixture
def client(own_books: None) -> Iterator[TestClient]:
    from recon.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("RECON_PASSWORD", "correct horse")
    get_settings.cache_clear()
    return "correct horse"


class TestNoPasswordYet:
    @pytest.mark.parametrize("path", PAGES)
    def test_your_own_books_stay_shut(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 503
        assert "RECON_PASSWORD" in response.text

    def test_the_health_check_still_answers(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_paystack_can_still_deliver(self, client: TestClient) -> None:
        raw = json.dumps(factories.charge_success()).encode()
        signature = hmac.new(b"sk_test_books", raw, hashlib.sha512).hexdigest()
        response = client.post(
            "/webhooks/paystack", content=raw, headers={SIGNATURE_HEADER: signature}
        )
        assert response.status_code == 200

    def test_the_demo_is_open_to_anyone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RECON_DEMO", "1")
        get_settings.cache_clear()
        assert client.get("/api/report").status_code == 200


class TestWithAPassword:
    @pytest.mark.parametrize("path", PAGES)
    def test_no_login_gets_the_browsers_login_box(
        self, client: TestClient, password: str, path: str
    ) -> None:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")

    def test_the_wrong_password_is_refused(self, client: TestClient, password: str) -> None:
        assert client.get("/review", auth=("ada", "wrong")).status_code == 401

    @pytest.mark.parametrize("path", PAGES)
    def test_the_right_password_opens_every_page(
        self, client: TestClient, password: str, path: str
    ) -> None:
        assert client.get(path, auth=("ada", password)).status_code == 200

    def test_the_name_typed_at_login_goes_on_the_decision(
        self, client: TestClient, password: str
    ) -> None:
        with session_scope() as session:
            session.add(
                Transaction(
                    reference="STMT-1",
                    channel=Channel.BANK_TRANSFER,
                    status=TransactionStatus.SUCCESS,
                    amount_kobo=500_000,
                    paid_at=datetime(2024, 5, 17, 12, tzinfo=UTC),
                    narration="FROM SOMEONE",
                )
            )
        response = client.post(
            "/review/STMT-1/reject",
            data={"reason": "not_a_payment"},
            auth=("  Ada  Okonkwo ", password),
            follow_redirects=False,
        )
        assert response.status_code == 303
        with session_scope() as session:
            decision = session.query(MatchDecision).one()
            assert decision.decided_by == "Ada Okonkwo"

    def test_the_demo_with_a_password_asks_for_it_too(
        self, client: TestClient, password: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RECON_DEMO", "1")
        get_settings.cache_clear()
        assert client.get("/api/report").status_code == 401
