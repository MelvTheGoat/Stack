"""The page where a business brings its own books in."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from recon import imports, state
from recon.config import Settings, get_settings
from recon.db import session_scope
from recon.enums import Layer
from recon.models import Order, Transaction
from recon.paystack.client import PaystackClient
from tests import factories

PASSWORD = "correct horse"


@pytest.fixture
def client(own_books: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from recon.app import app

    monkeypatch.setenv("RECON_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        test_client.auth = ("ada", PASSWORD)
        yield test_client


def upload(client: TestClient, path: str, body: str | bytes) -> Any:
    data = body.encode() if isinstance(body, str) else body
    return client.post(path, files={"file": ("upload.csv", data, "text/csv")})


class TestThePage:
    def test_it_shows_where_to_point_paystack(self, client: TestClient) -> None:
        response = client.get("/setup")
        assert response.status_code == 200
        assert "http://testserver/webhooks/paystack" in response.text

    def test_it_warns_when_the_data_would_not_survive_a_redeploy(self, client: TestClient) -> None:
        assert "wiped every time the app redeploys" in client.get("/setup").text

    def test_the_examples_download_as_csv(self, client: TestClient) -> None:
        for path, header in (
            ("/setup/invoices.csv", "reference,customer,amount,issued"),
            ("/setup/payments.csv", "date,amount,narration"),
        ):
            response = client.get(path)
            assert response.headers["content-type"].startswith("text/csv")
            assert response.text.startswith(header)

    def test_an_empty_queue_says_where_to_start(self, client: TestClient) -> None:
        page = client.get("/review").text
        assert "No payments yet" in page
        assert 'href="/setup"' in page
        assert "Practice data" not in page

    def test_refused_deliveries_are_counted_so_a_wrong_key_shows(self, client: TestClient) -> None:
        client.post("/webhooks/paystack", content=b"{}", headers={"x-paystack-signature": "no"})
        assert "1 delivery was refused" in client.get("/setup").text


class TestUploading:
    def test_invoices_then_payments_and_the_matches_follow(self, client: TestClient) -> None:
        response = upload(client, "/setup/invoices", imports.INVOICE_TEMPLATE)
        assert response.status_code == 200
        assert "Invoices: 2 added." in response.text

        response = upload(client, "/setup/payments", imports.PAYMENT_TEMPLATE)
        assert response.status_code == 200
        assert "Payments: 2 added." in response.text

        matches = {m.transaction_reference: m for m in state.workspace().matches}
        assert matches["FT24139XYZ"].order_references == ("INV-0001",)
        assert matches["FT24139XYZ"].layer is Layer.EXACT_REFERENCE

    def test_a_bad_file_is_refused_whole_with_its_line_numbers(self, client: TestClient) -> None:
        response = upload(
            client,
            "/setup/invoices",
            "reference,customer,amount,issued\nINV-1,Ada,500,2024-05-17\nINV-2,Musa,lots,2024-05-17\n",
        )
        assert response.status_code == 400
        assert "Nothing was imported" in response.text
        assert "line 3" in response.text
        with session_scope() as session:
            assert session.query(Order).count() == 0

    def test_a_file_too_big_is_refused_before_it_is_read(self, client: TestClient) -> None:
        response = upload(client, "/setup/payments", b"x" * (5 * 1024 * 1024 + 1))
        assert response.status_code == 413

    def test_the_demo_has_nowhere_to_put_anything(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RECON_DEMO", "1")
        get_settings.cache_clear()
        state.reset()
        response = upload(client, "/setup/invoices", imports.INVOICE_TEMPLATE)
        assert response.status_code == 409
        assert "Practice data" in client.get("/review").text


class TestPullingFromPaystack:
    def test_offline_it_says_so_and_pulls_nothing(self, client: TestClient) -> None:
        response = client.post("/setup/paystack", data={"since": "2024-05-01"})
        assert response.status_code == 502
        assert "RECON_OFFLINE" in response.text

    def test_a_pull_lands_in_the_books(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            rows = [factories.charge_success("ref_1")["data"]]
            return httpx.Response(
                200, json={"status": True, "data": rows, "meta": {"page": 1, "pageCount": 1}}
            )

        def fake(settings: Settings) -> PaystackClient:
            online = Settings(PAYSTACK_SECRET_KEY="sk_test_books", RECON_OFFLINE=False)
            return PaystackClient(
                online,
                httpx.Client(
                    transport=httpx.MockTransport(handler), base_url="https://api.paystack.co"
                ),
            )

        monkeypatch.setattr("recon.review.setup.PaystackClient", fake)
        response = client.post("/setup/paystack", data={"since": "2024-05-01"})
        assert response.status_code == 200
        assert "Paystack listed 1: 1 new" in response.text
        with session_scope() as session:
            assert session.get(Transaction, "ref_1") is not None
        assert [t.reference for t in state.workspace().transactions] == ["ref_1"]

    def test_a_date_that_is_not_one_is_refused(self, client: TestClient) -> None:
        response = client.post("/setup/paystack", data={"since": "last month"})
        assert response.status_code == 400

    def test_without_a_key_it_says_which_one_to_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PAYSTACK_SECRET_KEY")
        get_settings.cache_clear()
        response = client.post("/setup/paystack", data={"since": "2024-05-01"})
        assert response.status_code == 400
        assert "PAYSTACK_SECRET_KEY" in response.text
