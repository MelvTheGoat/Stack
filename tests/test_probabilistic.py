"""Layer two, and the pipeline that puts the layers in order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from recon.enums import Layer
from recon.match.calibration import IdentityCalibrator
from recon.match.candidates import shortlist
from recon.match.features import FEATURE_NAMES, PayerHistory, as_vector, extract
from recon.match.ledger import Ledger, TxnRow
from recon.match.model import LogisticModel
from recon.match.pipeline import Pipeline, layer_mix
from recon.match.probabilistic import ProbabilisticMatcher
from recon.money import Money

DAY = datetime(2024, 5, 17, 10, 0, tzinfo=UTC)


def build_ledger(
    orders: list[tuple[str, str, str, int]],
    customers: list[tuple[str, str]],
    accounts: dict[str, str] | None = None,
) -> Ledger:
    ledger = Ledger()
    ledger.add_customers([{"id": i, "name": n} for i, n in customers])
    ledger.add_orders(
        [
            {
                "reference": reference,
                "customer_id": customer,
                "amount_kobo": Money.from_naira(naira).kobo,
                "issued_at": (DAY - timedelta(days=days)).isoformat(),
                "status": "open",
            }
            for reference, customer, naira, days in orders
        ]
    )
    ledger.add_dedicated_accounts(
        [
            {"account_number": n, "bank": "Wema Bank", "customer_id": c}
            for n, c in (accounts or {}).items()
        ]
    )
    return ledger


def payment(
    reference: str = "T1",
    naira: str = "5000",
    *,
    narration: str = "",
    payer: str = "",
    account: str | None = None,
    channel: str = "bank_transfer",
    minutes: int = 0,
) -> TxnRow:
    return TxnRow(
        reference=reference,
        channel=channel,
        status="success",
        amount=Money.from_naira(naira),
        fees=Money.zero(),
        paid_at=DAY + timedelta(minutes=minutes),
        narration=narration,
        payer_name=payer,
        dva_account_number=account,
    )


class TestShortlisting:
    def test_a_name_in_the_narration_brings_in_that_customers_invoices(self) -> None:
        ledger = build_ledger(
            [("INV-1", "C1", "9000", 3), ("INV-2", "C2", "9000", 3)],
            [("C1", "Ada Okonkwo"), ("C2", "Chinedu Eze")],
        )
        found = shortlist(ledger, payment(naira="3000", narration="TRF FROM OKONKWO ADA"), set())
        assert "INV-1" in {ref for c in found for ref in c.order_references}

    def test_a_dedicated_account_brings_in_its_owners_invoices(self) -> None:
        ledger = build_ledger(
            [("INV-1", "C1", "9000", 3)],
            [("C1", "Ada Okonkwo")],
            accounts={"9911111111": "C1"},
        )
        found = shortlist(ledger, payment(naira="4000", account="9911111111"), set())
        assert any(c.via == "dva" for c in found)

    def test_three_invoices_that_add_up_exactly_are_offered_as_one_candidate(self) -> None:
        """The split-payment case. No single-invoice shortlist can catch it."""
        ledger = build_ledger(
            [("INV-1", "C1", "1000", 4), ("INV-2", "C1", "2000", 3), ("INV-3", "C1", "3000", 2)],
            [("C1", "Ada Okonkwo")],
        )
        found = shortlist(ledger, payment(naira="6000", payer="ADA OKONKWO"), set())
        combos = [c for c in found if c.is_combination]
        assert any(set(c.order_references) == {"INV-1", "INV-2", "INV-3"} for c in combos)

    def test_a_combination_that_is_one_kobo_out_is_not_offered(self) -> None:
        """Near-miss arithmetic is coincidence, not evidence."""
        ledger = build_ledger(
            [("INV-1", "C1", "1000", 4), ("INV-2", "C1", "2000", 3)],
            [("C1", "Ada Okonkwo")],
        )
        found = shortlist(ledger, payment(naira="3000.01", payer="ADA OKONKWO"), set())
        assert not [c for c in found if c.is_combination]

    def test_an_invoice_already_settled_is_not_offered_again(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 3)], [("C1", "Ada Okonkwo")])
        found = shortlist(ledger, payment(payer="ADA OKONKWO"), claimed={"INV-1"})
        assert not found

    def test_a_stranger_gets_an_empty_shortlist(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 3)], [("C1", "Ada Okonkwo")])
        found = shortlist(ledger, payment(naira="123", payer="MUSA DANJUMA"), set())
        assert not found


class TestFeatures:
    def test_every_declared_feature_is_produced(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 3)], [("C1", "Ada Okonkwo")])
        candidate = shortlist(ledger, payment(payer="ADA OKONKWO"), set())[0]
        features = extract(
            ledger,
            payment(payer="ADA OKONKWO"),
            candidate,
            candidate_count=1,
            open_orders_for_customer=1,
        )
        assert set(features) == set(FEATURE_NAMES)
        assert len(as_vector(features)) == len(FEATURE_NAMES)

    def test_an_exact_amount_and_a_matching_name_look_like_a_match(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 3)], [("C1", "Ada Okonkwo")])
        txn = payment(payer="OKONKWO ADA")
        candidate = shortlist(ledger, txn, set())[0]
        features = extract(ledger, txn, candidate, candidate_count=1, open_orders_for_customer=1)
        assert features["amount_exact"] == 1.0
        assert features["name_similarity"] > 0.9
        assert features["their_only_open_invoice"] == 1.0

    def test_an_underpayment_is_flagged_as_one(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 3)], [("C1", "Ada Okonkwo")])
        txn = payment(naira="2000", payer="OKONKWO ADA")
        candidate = shortlist(ledger, txn, set())[0]
        features = extract(ledger, txn, candidate, candidate_count=1, open_orders_for_customer=1)
        assert features["underpaid"] == 1.0
        assert features["overpaid"] == 0.0
        assert features["amount_closeness"] == pytest.approx(0.4)

    def test_recency_decays_with_age(self) -> None:
        ledger = build_ledger(
            [("INV-NEW", "C1", "5000", 1), ("INV-OLD", "C2", "5000", 30)],
            [("C1", "Ada Okonkwo"), ("C2", "Chinedu Eze")],
        )
        txn = payment()
        by_reference = {c.order_references[0]: c for c in shortlist(ledger, txn, set())}
        fresh = extract(
            ledger, txn, by_reference["INV-NEW"], candidate_count=2, open_orders_for_customer=1
        )
        stale = extract(
            ledger, txn, by_reference["INV-OLD"], candidate_count=2, open_orders_for_customer=1
        )
        assert fresh["recency"] > stale["recency"]
        assert fresh["inside_the_usual_window"] == 1.0
        assert stale["inside_the_usual_window"] == 0.0


class TestPayerHistory:
    def test_a_name_we_have_seen_pay_for_them_before_scores_high(self) -> None:
        history = PayerHistory()
        history.record("C1", "OKONKWO ADA")
        assert history.familiarity("C1", "TRF FROM OKONKWO ADA") > 0.9

    def test_a_customer_we_know_nothing_about_scores_zero(self) -> None:
        assert PayerHistory().familiarity("C1", "OKONKWO ADA") == 0.0


class TestMatching:
    def matcher(self, ledger: Ledger, threshold: float = 0.5) -> ProbabilisticMatcher:
        """A model that just reports the amount-closeness feature, so the tests
        exercise the matcher's plumbing rather than a particular fit."""
        weights = [0.0] * len(FEATURE_NAMES)
        weights[FEATURE_NAMES.index("amount_closeness")] = 8.0
        weights[FEATURE_NAMES.index("name_similarity")] = 4.0
        model = LogisticModel(FEATURE_NAMES, weights=weights, bias=-6.0)
        return ProbabilisticMatcher(
            ledger=ledger, model=model, calibrator=IdentityCalibrator(), threshold=threshold
        )

    def test_a_confident_match_is_cleared_and_labelled_probabilistic(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        match = self.matcher(ledger, threshold=0.5).match(payment(payer="OKONKWO ADA"), set())
        assert match.layer is Layer.PROBABILISTIC
        assert match.order_references == ("INV-1",)
        assert match.resolved

    def test_below_the_line_it_goes_to_a_person_with_its_evidence(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        match = self.matcher(ledger, threshold=0.999).match(payment(payer="OKONKWO ADA"), set())
        assert match.layer is Layer.UNRESOLVED
        assert match.needs_human
        assert match.order_references == (), "an unsure answer claims nothing"
        assert match.evidence["candidates"], "a person needs to see what we were weighing"
        assert "below the" in match.evidence["reason"]

    def test_the_evidence_is_written_in_words_a_bookkeeper_can_check(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        match = self.matcher(ledger, threshold=0.999).match(payment(payer="OKONKWO ADA"), set())
        reasons = match.evidence["candidates"][0]["why"]
        assert any("name" in reason for reason in reasons)
        assert any("amount" in reason for reason in reasons)

    def test_nothing_to_match_against_goes_to_a_person(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        match = self.matcher(ledger).match(payment(naira="17", payer="MUSA DANJUMA"), set())
        assert match.layer is Layer.UNRESOLVED
        assert "no invoice" in match.evidence["reason"]

    def test_the_runner_up_gap_is_reported(self) -> None:
        ledger = build_ledger(
            [("INV-1", "C1", "5000", 2), ("INV-2", "C2", "5000", 2)],
            [("C1", "Ada Okonkwo"), ("C2", "Ada Okonkwa")],
        )
        match = self.matcher(ledger, threshold=0.999).match(payment(payer="OKONKWO ADA"), set())
        assert match.evidence["runner_up_gap"] is not None


class TestPipelineOrder:
    def test_the_cheap_layer_goes_first_and_the_model_never_sees_its_work(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        never_called = ProbabilisticMatcher(
            ledger=ledger,
            model=LogisticModel(FEATURE_NAMES),
            calibrator=IdentityCalibrator(),
            threshold=0.0,
        )
        pipeline = Pipeline.build(ledger, never_called)
        matches = pipeline.run([payment(payer="ADA OKONKWO")])
        assert matches[0].layer is Layer.AMOUNT_WINDOW, "layer two must not do layer one's job"

    def test_an_invoice_taken_by_layer_one_is_off_the_table_for_layer_two(self) -> None:
        ledger = build_ledger(
            [("INV-1", "C1", "5000", 2), ("INV-2", "C1", "3000", 2)],
            [("C1", "Ada Okonkwo")],
        )
        weights = [0.0] * len(FEATURE_NAMES)
        weights[FEATURE_NAMES.index("name_similarity")] = 10.0
        matcher = ProbabilisticMatcher(
            ledger=ledger,
            model=LogisticModel(FEATURE_NAMES, weights=weights, bias=-2.0),
            calibrator=IdentityCalibrator(),
            threshold=0.5,
        )
        pipeline = Pipeline.build(ledger, matcher)
        matches = pipeline.run(
            [
                payment("T1", naira="5000", payer="ADA OKONKWO"),
                payment("T2", naira="2500", payer="ADA OKONKWO", minutes=48 * 60),
            ]
        )
        by_reference = {m.transaction_reference: m for m in matches}
        assert by_reference["T1"].order_references == ("INV-1",)
        assert "INV-1" not in by_reference["T2"].order_references

    def test_the_layer_mix_adds_up(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        matches = Pipeline.build(ledger).run(
            [payment("T1", payer="ADA OKONKWO"), payment("T2", naira="17")]
        )
        mix = layer_mix(matches)
        assert sum(mix.values()) == pytest.approx(1.0)
        assert mix["deterministic"] == 0.5
        assert mix["human"] == 0.5

    def test_without_a_model_the_pipeline_is_just_layer_one(self) -> None:
        ledger = build_ledger([("INV-1", "C1", "5000", 2)], [("C1", "Ada Okonkwo")])
        matches = Pipeline.build(ledger).run([payment(naira="17")])
        assert matches[0].layer is Layer.UNRESOLVED
