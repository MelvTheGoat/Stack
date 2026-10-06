"""Who may open the pages: logins, sign-ups, and what only an admin sees."""

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
from recon.enums import AccountStatus, Channel, TransactionStatus
from recon.models import MatchDecision, Transaction, User
from recon.paystack.signature import SIGNATURE_HEADER
from tests import factories
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

PAGES = ("/review", "/report", "/setup")
API = ("/api/queue", "/api/report", "/api/labels")
STAFF_PASSWORD = "ada's long password"


@pytest.fixture
def client(own_books: None) -> Iterator[TestClient]:
    """No admin configured at all."""
    from recon.app import app

    with TestClient(app) as test_client:
        yield test_client


def sign_up_and_let_in(admin: TestClient, email: str = "ada@example.com") -> TestClient:
    """A second browser: Ada signs up, the admin lets her in, she logs in."""
    from recon.app import app

    ada = TestClient(app)
    ada.post("/signup", data={"name": "Ada Okonkwo", "email": email, "password": STAFF_PASSWORD})
    with session_scope() as session:
        user_id = session.query(User).filter(User.email == email).one().id
    admin.post(f"/admin/people/{user_id}/approve")
    response = ada.post(
        "/login", data={"email": email, "password": STAFF_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return ada


def a_payment_waiting() -> None:
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


class TestBeforeThereIsAnAdmin:
    @pytest.mark.parametrize("path", PAGES + API)
    def test_your_own_books_stay_shut(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 503
        assert "RECON_ADMIN_EMAIL" in response.text

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
        assert client.get("/review").status_code == 200


class TestLoggingIn:
    @pytest.mark.parametrize("path", PAGES)
    def test_a_page_sends_you_to_the_login_box_and_back(
        self, admin_client: TestClient, path: str
    ) -> None:
        admin_client.post("/logout")
        response = admin_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/login?next={path}"

    @pytest.mark.parametrize("path", API)
    def test_the_api_says_401_rather_than_redirecting(
        self, admin_client: TestClient, path: str
    ) -> None:
        admin_client.post("/logout")
        assert admin_client.get(path).status_code == 401

    @pytest.mark.parametrize("path", PAGES + API)
    def test_logged_in_every_page_opens(self, admin_client: TestClient, path: str) -> None:
        assert admin_client.get(path).status_code == 200

    def test_the_wrong_password_says_so_without_saying_which_part(
        self, admin_client: TestClient
    ) -> None:
        admin_client.post("/logout")
        response = admin_client.post(
            "/login", data={"email": ADMIN_EMAIL, "password": "wrong wrong wrong"}
        )
        assert response.status_code == 401
        assert "do not match an account" in response.text

    def test_the_cookie_cannot_be_read_by_scripts(
        self, own_books: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from recon.app import app

        monkeypatch.setenv("RECON_ADMIN_EMAIL", ADMIN_EMAIL)
        monkeypatch.setenv("RECON_PASSWORD", ADMIN_PASSWORD)
        get_settings.cache_clear()
        with TestClient(app) as client:
            response = client.post(
                "/login",
                data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                follow_redirects=False,
            )
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie

    def test_after_login_you_go_where_you_were_going_but_only_on_this_site(
        self, admin_client: TestClient
    ) -> None:
        for asked, sent in (("/report", "/report"), ("//evil.example", "/review")):
            response = admin_client.post(
                "/login",
                data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": asked},
                follow_redirects=False,
            )
            assert response.headers["location"] == sent

    def test_logging_out_ends_it(self, admin_client: TestClient) -> None:
        admin_client.post("/logout")
        assert admin_client.get("/review", follow_redirects=False).status_code == 303

    def test_a_decision_is_signed_with_the_email_of_whoever_made_it(
        self, admin_client: TestClient
    ) -> None:
        a_payment_waiting()
        ada = sign_up_and_let_in(admin_client)
        response = ada.post(
            "/review/STMT-1/reject", data={"reason": "not_a_payment"}, follow_redirects=False
        )
        assert response.status_code == 303
        with session_scope() as session:
            assert session.query(MatchDecision).one().decided_by == "ada@example.com"


class TestSigningUp:
    def test_a_new_account_waits_until_an_admin_lets_it_in(self, admin_client: TestClient) -> None:
        from recon.app import app

        newcomer = TestClient(app)
        page = newcomer.post(
            "/signup",
            data={"name": "Ada", "email": "ada@example.com", "password": STAFF_PASSWORD},
        )
        assert "An admin has to let you in" in page.text
        refused = newcomer.post(
            "/login", data={"email": "ada@example.com", "password": STAFF_PASSWORD}
        )
        assert "waiting for an admin" in refused.text

        people = admin_client.get("/admin/people").text
        assert "ada@example.com" in people and "pending" in people
        assert 'class="badge">1<' in admin_client.get("/review").text

    def test_a_password_too_short_is_refused_on_the_page(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/signup", data={"name": "Ada", "email": "ada@example.com", "password": "short"}
        )
        assert response.status_code == 400
        assert "10 characters" in response.text


class TestWhatOnlyAnAdminSees:
    @pytest.mark.parametrize("path", ["/admin/people", "/admin/activity"])
    def test_staff_are_turned_away(self, admin_client: TestClient, path: str) -> None:
        ada = sign_up_and_let_in(admin_client)
        assert ada.get(path).status_code == 403
        assert "/admin/people" not in ada.get("/review").text, "no admin link in staff's nav"

    def test_removing_someone_takes_effect_on_their_next_click(
        self, admin_client: TestClient
    ) -> None:
        ada = sign_up_and_let_in(admin_client)
        assert ada.get("/review").status_code == 200
        with session_scope() as session:
            user_id = session.query(User).filter(User.email == "ada@example.com").one().id
        admin_client.post(f"/admin/people/{user_id}/remove")
        assert ada.get("/review", follow_redirects=False).status_code == 303
        with session_scope() as session:
            assert session.get(User, user_id).status is AccountStatus.DISABLED  # type: ignore[union-attr]

    def test_the_admin_cannot_remove_the_last_admin(self, admin_client: TestClient) -> None:
        with session_scope() as session:
            me = session.query(User).filter(User.email == ADMIN_EMAIL).one().id
        response = admin_client.post(f"/admin/people/{me}/remove")
        assert response.status_code == 400
        assert "nobody who can let people in" in response.text

    def test_the_activity_page_shows_who_did_what(self, admin_client: TestClient) -> None:
        a_payment_waiting()
        ada = sign_up_and_let_in(admin_client)
        ada.post("/review/STMT-1/reject", data={"reason": "not_a_payment"})

        everything = admin_client.get("/admin/activity").text
        assert "signed up" in everything
        assert "user approve" in everything
        assert "rejected" in everything

        only_ada = admin_client.get("/admin/activity?who=ada@example.com").text
        assert "rejected" in only_ada
        assert "user approve" not in only_ada, "that was the admin's doing, not Ada's"


def test_you_can_change_the_name_others_see(admin_client: TestClient) -> None:
    admin_client.post("/account/name", data={"name": "Melvyn"})
    assert "Melvyn" in admin_client.get("/review").text
    with session_scope() as session:
        assert session.query(User).filter(User.email == ADMIN_EMAIL).one().name == "Melvyn"
