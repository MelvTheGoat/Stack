"""The review queue, the decisions people make on it, and the two pages."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recon.enums import DecisionStatus, Layer, RejectReason
from recon.match.ledger import TxnRow
from recon.match.result import Match
from recon.models import AuditRecord, MatchDecision
from recon.money import Money
from recon.review import queue as review_queue

WHEN = datetime(2024, 5, 17, 12, 0, tzinfo=UTC)


def payment(reference: str, naira: str, narration: str = "TRF FROM ADA") -> TxnRow:
    return TxnRow(
        reference=reference,
        channel="bank_transfer",
        status="success",
        amount=Money.from_naira(naira),
        fees=Money.zero(),
        paid_at=WHEN,
        narration=narration,
        payer_name="ADA OKONKWO",
    )


def queued(reference: str, naira: str, confidence: float = 0.3, **evidence: object) -> Match:
    return Match(
        transaction_reference=reference,
        order_references=(),
        layer=Layer.UNRESOLVED,
        confidence=confidence,
        evidence={"reason": "below the line", **evidence},
        needs_human=True,
        money_at_risk=Money.from_naira(naira),
    )


class TestOrdering:
    def test_the_biggest_money_comes_first(self) -> None:
        transactions = [payment("T1", "2000"), payment("T2", "400000"), payment("T3", "50000")]
        matches = [queued("T1", "2000"), queued("T2", "400000"), queued("T3", "50000")]
        queue = review_queue.build(matches, transactions)
        assert [item.transaction_reference for item in queue.items] == ["T2", "T3", "T1"]

    def test_settled_cases_are_not_in_the_queue(self) -> None:
        transactions = [payment("T1", "2000"), payment("T2", "5000")]
        matches = [
            Match(
                "T1", ("INV-1",), Layer.EXACT_REFERENCE, 1.0, money_at_risk=Money.from_naira("2000")
            ),
            queued("T2", "5000"),
        ]
        queue = review_queue.build(matches, transactions)
        assert [item.transaction_reference for item in queue.items] == ["T2"]

    def test_an_evenings_worth_covers_more_than_its_share_of_the_money(self) -> None:
        """Twenty cases worth ₦1,000 to ₦20,000. The first five are a quarter of
        the work and should be well over a quarter of the money — that is the
        whole reason for sorting this way."""
        transactions = [payment(f"T{i}", str(1000 * i)) for i in range(1, 21)]
        matches = [queued(f"T{i}", str(1000 * i)) for i in range(1, 21)]
        queue = review_queue.build(matches, transactions)

        share_of_money = queue.money_in_the_top(5).kobo / queue.money_at_risk.kobo
        assert share_of_money > 0.25 * 1.5
        assert queue.money_in_the_top(5) == Money.from_naira("90000")

    def test_the_total_at_risk_adds_up(self) -> None:
        transactions = [payment("T1", "2000"), payment("T2", "3000")]
        queue = review_queue.build([queued("T1", "2000"), queued("T2", "3000")], transactions)
        assert queue.money_at_risk == Money.from_naira("5000")


class TestWhatAReviewerSees:
    def test_the_candidates_and_their_reasons_come_through(self) -> None:
        match = queued(
            "T1",
            "5000",
            candidates=[
                {
                    "orders": ["INV-1"],
                    "customer": "Ada Okonkwo",
                    "invoice_total": "₦5,000.00",
                    "confidence": 0.62,
                    "found_because": "name",
                    "why": ["the name matches", "the amount is exact"],
                }
            ],
        )
        item = review_queue.build([match], [payment("T1", "5000")]).items[0]
        assert item.has_a_suggestion
        assert item.candidates[0].order_references == ("INV-1",)
        assert "the name matches" in item.candidates[0].why

    def test_a_suspected_repeat_says_what_it_repeats(self) -> None:
        match = queued("T2", "5000", duplicate_of="T1")
        item = review_queue.build([match], [payment("T2", "5000")]).items[0]
        assert item.is_suspected_duplicate
        assert item.duplicate_of == "T1"

    def test_a_case_with_nothing_to_suggest_still_shows_the_narration(self) -> None:
        item = review_queue.build(
            [queued("T1", "5000")], [payment("T1", "5000", narration="NIP/KUDA/UNKNOWN/TRF")]
        ).items[0]
        assert not item.has_a_suggestion
        assert item.narration == "NIP/KUDA/UNKNOWN/TRF"


class TestDecisions:
    def item(self) -> review_queue.QueueItem:
        return review_queue.build([queued("T1", "5000", 0.42)], [payment("T1", "5000")]).items[0]

    def test_approving_records_who_said_yes(self, session: Session) -> None:
        review_queue.approve(session, self.item(), ["INV-1"], who="melvyn")
        session.commit()

        decision = session.query(MatchDecision).one()
        assert decision.status is DecisionStatus.APPROVED
        assert decision.order_references == ["INV-1"]
        assert decision.layer is Layer.HUMAN
        assert decision.decided_by == "melvyn"

    def test_rejecting_stores_the_reason_as_a_label(self, session: Session) -> None:
        review_queue.reject(session, self.item(), RejectReason.WRONG_CUSTOMER, who="melvyn")
        session.commit()

        decision = session.query(MatchDecision).one()
        assert decision.status is DecisionStatus.REJECTED
        assert decision.reject_reason is RejectReason.WRONG_CUSTOMER
        assert decision.order_references == [], "a rejection settles nothing"

    def test_a_rejection_keeps_what_the_model_had_suggested(self, session: Session) -> None:
        """That is the label. It says which mistake the model made, on a case it
        got wrong, and those are much rarer than approvals."""
        match = queued(
            "T1",
            "5000",
            0.42,
            candidates=[{"orders": ["INV-9"], "customer": "Ada", "confidence": 0.42, "why": []}],
        )
        item = review_queue.build([match], [payment("T1", "5000")]).items[0]
        review_queue.reject(session, item, RejectReason.WRONG_CUSTOMER, who="melvyn")
        session.commit()

        labels = review_queue.rejection_labels(session)
        assert labels[0]["rejected_suggestion"] == [["INV-9"]]
        assert labels[0]["model_confidence"] == pytest.approx(0.42)
        assert labels[0]["reason"] == "wrong_customer"

    def test_every_decision_writes_an_audit_row_with_a_name_on_it(self, session: Session) -> None:
        review_queue.approve(session, self.item(), ["INV-1"], who="melvyn")
        session.commit()

        entry = session.query(AuditRecord).one()
        assert entry.action == "approved"
        assert entry.actor == "melvyn"
        assert entry.layer is Layer.HUMAN
        assert "INV-1" in entry.evidence_json

    def test_changing_your_mind_leaves_both_audit_rows(self, session: Session) -> None:
        item = self.item()
        review_queue.approve(session, item, ["INV-1"], who="melvyn")
        review_queue.reject(session, item, RejectReason.WRONG_CUSTOMER, who="melvyn")
        session.commit()

        assert session.query(MatchDecision).count() == 1, "one current answer"
        assert session.query(AuditRecord).count() == 2, "but the whole story is kept"


class TestThePages:
    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        from recon import db as db_module
        from recon import state
        from recon.app import app
        from recon.config import get_settings

        os.environ["RECON_DATABASE_URL"] = f"sqlite+pysqlite:///{tmp_path / 'review.db'}"
        os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_review"
        os.environ["RECON_OFFLINE"] = "1"
        get_settings.cache_clear()
        db_module.reset_engine()
        state.reset()

        with TestClient(app) as test_client:
            yield test_client

        state.reset()
        db_module.reset_engine()
        get_settings.cache_clear()

    def test_the_queue_page_renders(self, client: TestClient) -> None:
        response = client.get("/review")
        assert response.status_code == 200
        assert "Money at risk" in response.text

    def test_the_report_page_renders(self, client: TestClient) -> None:
        response = client.get("/report")
        assert response.status_code == 200
        assert "Why the two numbers differ" in response.text

    def test_the_queue_api_is_ordered_by_money(self, client: TestClient) -> None:
        items = client.get("/api/queue").json()["items"]
        amounts = [
            Money.from_naira(item["paid"].replace("₦", "").replace(",", "")) for item in items
        ]
        assert amounts == sorted(amounts, reverse=True)

    def test_rejecting_through_the_page_stores_a_label(self, client: TestClient) -> None:
        first = client.get("/api/queue").json()["items"][0]
        before = len(client.get("/api/queue").json()["items"])

        response = client.post(
            f"/review/{first['transaction']}/reject",
            data={"reason": "not_a_payment"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        labels = client.get("/api/labels").json()["labels"]
        assert labels[0]["transaction_reference"] == first["transaction"]
        assert labels[0]["reason"] == "not_a_payment"
        assert len(client.get("/api/queue").json()["items"]) == before - 1

    def test_approving_through_the_page_takes_it_off_the_queue(self, client: TestClient) -> None:
        first = client.get("/api/queue").json()["items"][0]
        client.post(
            f"/review/{first['transaction']}/approve",
            data={"orders": "INV-0001"},
            follow_redirects=False,
        )
        remaining = [i["transaction"] for i in client.get("/api/queue").json()["items"]]
        assert first["transaction"] not in remaining

    def test_the_report_api_balances(self, client: TestClient) -> None:
        assert client.get("/api/report").json()["balances"] is True
