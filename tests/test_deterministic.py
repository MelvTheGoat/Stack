"""Layer one must be certain or silent. These tests mostly check it stays silent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from recon.enums import Layer
from recon.match.deterministic import DeterministicMatcher, coverage, residual
from recon.match.ledger import Ledger, TxnRow
from recon.money import Money

DAY = datetime(2024, 5, 17, 10, 0, tzinfo=UTC)


def ledger_with(
    *orders: tuple[str, str, str, int], accounts: dict[str, str] | None = None
) -> Ledger:
    """orders are (reference, customer_id, naira, days_before_payment)."""
    ledger = Ledger()
    ledger.add_customers(
        [
            {"id": "CUS_1", "name": "Ada Okonkwo"},
            {"id": "CUS_2", "name": "Chinedu Eze"},
        ]
    )
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
            {"account_number": number, "bank": "Wema Bank", "customer_id": customer}
            for number, customer in (accounts or {}).items()
        ]
    )
    return ledger


def payment(
    reference: str = "T1",
    naira: str = "5000",
    *,
    minutes: int = 0,
    narration: str = "",
    payer: str = "ADA OKONKWO",
    stated: str | None = None,
    account: str | None = None,
    channel: str = "bank_transfer",
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
        stated_reference=stated,
        dva_account_number=account,
    )


class TestExactReference:
    def test_a_structured_reference_closes_the_case(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        match = DeterministicMatcher(ledger).match(payment(stated="INV-0001"))
        assert match.order_references == ("INV-0001",)
        assert match.layer is Layer.EXACT_REFERENCE
        assert match.confidence == 1.0
        assert match.resolved

    def test_a_reference_typed_into_the_narration_is_found(self) -> None:
        ledger = ledger_with(("INV-0042", "CUS_1", "5000", 1))
        match = DeterministicMatcher(ledger).match(
            payment(narration="NIP/GTB/ADA OKONKWO/PAYMENT FOR INV-0042")
        )
        assert match.order_references == ("INV-0042",)
        assert match.evidence["found_in"] == "narration"

    def test_a_reference_shaped_string_that_is_not_ours_is_ignored(self) -> None:
        """The narration is full of session ids and bank codes. Only real invoice
        numbers count, and "real" means present in the ledger.

        The amount is deliberately one nobody is owed, so nothing further down
        can rescue the case and the reference rule is the only thing on trial.
        """
        ledger = ledger_with(("INV-0042", "CUS_1", "5000", 1))
        match = DeterministicMatcher(ledger).match(
            payment(naira="777", narration="NIP/GTB/ADA OKONKWO/REF-9999 TRF-12345")
        )
        assert match.layer is Layer.UNRESOLVED

    def test_two_references_in_one_narration_is_not_certain(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 1), ("INV-0002", "CUS_1", "3000", 1))
        match = DeterministicMatcher(ledger).match(
            payment(naira="8000", narration="PAYMENT INV-0001 AND INV-0002")
        )
        assert match.layer is Layer.UNRESOLVED, "a split payment is not a certainty"

    def test_a_reference_match_is_reported_even_when_the_amount_is_short(self) -> None:
        """If the payer named the invoice, they told us which invoice it is. The
        short amount is a separate fact, recorded as evidence."""
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 1))
        match = DeterministicMatcher(ledger).match(payment(naira="2000", stated="INV-0001"))
        assert match.order_references == ("INV-0001",)
        assert match.evidence["amount_matches"] is False


class TestDedicatedAccount:
    def test_money_in_a_customers_own_account_is_that_customers_money(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 1), accounts={"9911111111": "CUS_1"})
        match = DeterministicMatcher(ledger).match(payment(account="9911111111"))
        assert match.layer is Layer.DVA_ATTRIBUTION
        assert match.order_references == ("INV-0001",)
        assert match.evidence["customer_name"] == "Ada Okonkwo"

    def test_it_does_not_fire_when_the_customer_has_two_invoices_of_that_size(self) -> None:
        ledger = ledger_with(
            ("INV-0001", "CUS_1", "5000", 1),
            ("INV-0002", "CUS_1", "5000", 1),
            accounts={"9911111111": "CUS_1"},
        )
        match = DeterministicMatcher(ledger).match(payment(account="9911111111"))
        assert match.layer is Layer.UNRESOLVED, "knowing who is not knowing which"

    def test_it_does_not_fire_when_the_amount_matches_nothing(self) -> None:
        """A part payment into a dedicated account: we know the customer, and we
        still cannot say which invoice. That is the fuzzy layer's job."""
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 1), accounts={"9911111111": "CUS_1"})
        match = DeterministicMatcher(ledger).match(payment(naira="2000", account="9911111111"))
        assert match.layer is Layer.UNRESOLVED

    def test_an_unknown_account_number_tells_us_nothing(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 1), accounts={"9911111111": "CUS_1"})
        match = DeterministicMatcher(ledger).match(payment(naira="777", account="9999999999"))
        assert match.layer is Layer.UNRESOLVED


class TestAmountAndWindow:
    def test_one_invoice_at_exactly_this_amount_closes_the_case(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        match = DeterministicMatcher(ledger).match(payment())
        assert match.layer is Layer.AMOUNT_WINDOW
        assert match.order_references == ("INV-0001",)

    def test_two_invoices_at_the_same_amount_is_a_tie_and_ties_go_to_a_person(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2), ("INV-0002", "CUS_2", "5000", 2))
        match = DeterministicMatcher(ledger).match(payment())
        assert match.layer is Layer.UNRESOLVED

    def test_one_kobo_out_is_not_an_exact_amount(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        match = DeterministicMatcher(ledger).match(payment(naira="5000.01"))
        assert match.layer is Layer.UNRESOLVED

    def test_an_invoice_older_than_the_window_is_not_a_candidate(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 40))
        match = DeterministicMatcher(ledger).match(payment())
        assert match.layer is Layer.UNRESOLVED

    def test_you_cannot_pay_an_invoice_that_does_not_exist_yet(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", -5))
        match = DeterministicMatcher(ledger).match(payment())
        assert match.layer is Layer.UNRESOLVED

    def test_an_invoice_already_settled_is_not_available_again(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2), ("INV-0002", "CUS_2", "5000", 2))
        matcher = DeterministicMatcher(ledger)
        first = matcher.match(payment("T1", stated="INV-0001"))
        second = matcher.match(payment("T2", minutes=48 * 60, payer="CHINEDU EZE"))

        assert first.order_references == ("INV-0001",)
        assert second.order_references == ("INV-0002",), "the tie broke once one was taken"


class TestDuplicates:
    def test_the_same_payment_twice_in_a_day_pays_nothing_new(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        matcher = DeterministicMatcher(ledger)
        first = matcher.match(payment("T1", stated="INV-0001"))
        second = matcher.match(payment("T2", minutes=30, stated="INV-0001"))

        assert first.order_references == ("INV-0001",)
        assert second.order_references == ()
        assert second.evidence["duplicate_of"] == "T1"
        assert second.needs_human, "give money back or bank it is a person's call"
        assert second.money_at_risk == Money.from_naira("5000")

    def test_the_same_amount_a_week_later_is_a_second_purchase(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2), ("INV-0002", "CUS_1", "5000", 0))
        matcher = DeterministicMatcher(ledger)
        matcher.match(payment("T1", stated="INV-0001"))
        second = matcher.match(payment("T2", minutes=7 * 24 * 60, stated="INV-0002"))
        assert second.order_references == ("INV-0002",)

    def test_two_unmatched_payments_of_the_same_size_are_both_still_open(self) -> None:
        """Neither settled anything, so neither is evidence that the other is a copy."""
        ledger = ledger_with(("INV-0001", "CUS_1", "999", 2))
        matcher = DeterministicMatcher(ledger)
        first = matcher.match(payment("T1", naira="7777"))
        second = matcher.match(payment("T2", naira="7777", minutes=10))
        assert first.layer is Layer.UNRESOLVED
        assert second.layer is Layer.UNRESOLVED
        assert "duplicate_of" not in second.evidence


class TestBatchBehaviour:
    def test_a_batch_is_worked_oldest_first_whatever_order_it_arrives_in(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        late = payment("T_late", minutes=60, stated="INV-0001")
        early = payment("T_early", minutes=0, stated="INV-0001")

        matches = {
            m.transaction_reference: m
            for m in DeterministicMatcher(ledger).match_all([late, early])
        }
        assert matches["T_early"].order_references == ("INV-0001",)
        assert matches["T_late"].order_references == ()

    def test_coverage_adds_up_to_one(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2), ("INV-0002", "CUS_1", "3000", 2))
        matches = DeterministicMatcher(ledger).match_all(
            [
                payment("T1", stated="INV-0001"),
                payment("T2", naira="99"),
                payment("T3", naira="3000"),
            ]
        )
        assert sum(coverage(matches).values()) == pytest.approx(1.0)

    def test_residual_lists_exactly_the_unresolved_ones(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        matches = DeterministicMatcher(ledger).match_all(
            [payment("T1", stated="INV-0001"), payment("T2", naira="99")]
        )
        assert residual(matches) == ["T2"]


class TestResultInvariants:
    def test_confidence_outside_zero_to_one_is_refused(self) -> None:
        from recon.match.result import Match

        with pytest.raises(ValueError, match="between 0 and 1"):
            Match("T1", (), Layer.EXACT_REFERENCE, 1.5)

    def test_every_deterministic_match_is_fully_confident(self) -> None:
        ledger = ledger_with(("INV-0001", "CUS_1", "5000", 2))
        match = DeterministicMatcher(ledger).match(payment(stated="INV-0001"))
        assert match.confidence == 1.0, "layer one does not do maybes"
