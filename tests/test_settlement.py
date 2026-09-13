"""The daily report. Its job is to explain a difference, not hide one."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from recon.enums import Layer
from recon.match.ledger import TxnRow
from recon.match.result import Match
from recon.money import Money
from recon.report.settlement import build, summarise

FRIDAY = date(2024, 5, 17)
MONDAY = date(2024, 5, 20)


def payment(
    reference: str,
    naira: str,
    channel: str,
    *,
    day: date = FRIDAY,
    fees: str = "0",
    refunded: str = "0",
    status: str = "success",
    settlement_id: str | None = None,
    verified: bool = True,
) -> TxnRow:
    return TxnRow(
        reference=reference,
        channel=channel,
        status=status,
        amount=Money.from_naira(naira),
        fees=Money.from_naira(fees),
        paid_at=datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC),
        refunded=Money.from_naira(refunded),
        settlement_id=settlement_id,
        verified=verified,
    )


def batch(
    batch_id: str, day: date, gross: str, fees: str, net: str, count: int = 1
) -> dict[str, Any]:
    return {
        "id": batch_id,
        "settled_at": f"{day.isoformat()}T14:00:00+00:00",
        "gross_kobo": Money.from_naira(gross).kobo,
        "fees_kobo": Money.from_naira(fees).kobo,
        "net_kobo": Money.from_naira(net).kobo,
        "transaction_count": count,
    }


class TestTakings:
    def test_gross_fees_and_net(self) -> None:
        report = build(
            FRIDAY,
            [payment("T1", "10000", "card", fees="250"), payment("T2", "5000", "cash")],
            [],
        )
        assert report.gross_taken == Money.from_naira("15000")
        assert report.fees_charged == Money.from_naira("250")
        assert report.net_earned == Money.from_naira("14750")

    def test_channels_are_broken_out(self) -> None:
        report = build(
            FRIDAY,
            [
                payment("T1", "10000", "card", fees="250"),
                payment("T2", "5000", "cash"),
                payment("T3", "8000", "bank_transfer"),
            ],
            [],
        )
        assert {str(line.channel) for line in report.taken} == {"card", "cash", "bank_transfer"}
        assert sum(line.count for line in report.taken) == 3

    def test_a_failed_payment_is_not_income(self) -> None:
        report = build(FRIDAY, [payment("T1", "10000", "card", status="failed")], [])
        assert report.gross_taken.is_zero()

    def test_a_reversal_is_counted_separately_not_as_income(self) -> None:
        report = build(FRIDAY, [payment("T1", "10000", "bank_transfer", status="reversed")], [])
        assert report.gross_taken.is_zero()
        assert report.reversals == Money.from_naira("10000")

    def test_other_days_are_not_in_todays_report(self) -> None:
        report = build(FRIDAY, [payment("T1", "10000", "card", day=MONDAY)], [])
        assert report.gross_taken.is_zero()


class TestTheTimingGap:
    def test_cash_is_already_here_and_card_is_not(self) -> None:
        report = build(
            FRIDAY,
            [payment("T1", "10000", "card", fees="250"), payment("T2", "5000", "cash")],
            [],
        )
        assert report.cash_in_hand == Money.from_naira("5000")
        assert report.still_in_the_air == Money.from_naira("9750")

    def test_a_main_account_transfer_never_goes_through_paystack(self) -> None:
        report = build(FRIDAY, [payment("T1", "8000", "bank_transfer")], [])
        assert report.cash_in_hand == Money.from_naira("8000")
        assert report.still_in_the_air.is_zero()

    def test_everything_taken_is_either_here_or_coming(self) -> None:
        report = build(
            FRIDAY,
            [
                payment("T1", "10000", "card", fees="250"),
                payment("T2", "5000", "cash"),
                payment("T3", "8000", "bank_transfer"),
                payment("T4", "3000", "dva", fees="30"),
            ],
            [],
        )
        assert report.balances
        assert report.cash_in_hand + report.still_in_the_air == report.net_earned


class TestWhatLandedInTheBank:
    def test_a_batch_shows_the_day_it_was_earned(self) -> None:
        transactions = [payment("T1", "10000", "card", fees="250", settlement_id="STL_1")]
        report = build(MONDAY, transactions, [], [batch("STL_1", MONDAY, "10000", "250", "9750")])
        assert len(report.received) == 1
        line = report.received[0]
        assert line.earned_on == FRIDAY
        assert line.days_late == 3, "Friday's money lands on Monday"
        assert report.received_today == Money.from_naira("9750")

    def test_a_batch_that_does_not_match_its_transactions_is_a_problem(self) -> None:
        transactions = [payment("T1", "10000", "card", fees="250", settlement_id="STL_1")]
        report = build(MONDAY, transactions, [], [batch("STL_1", MONDAY, "99999", "250", "99749")])
        kinds = {problem["kind"] for problem in report.problems}
        assert "settlement_does_not_match_its_transactions" in kinds

    def test_a_batch_whose_own_arithmetic_is_wrong_is_a_problem(self) -> None:
        transactions = [payment("T1", "10000", "card", fees="250", settlement_id="STL_1")]
        report = build(MONDAY, transactions, [], [batch("STL_1", MONDAY, "10000", "250", "10000")])
        kinds = {problem["kind"] for problem in report.problems}
        assert "settlement_net_is_not_gross_minus_fees" in kinds

    def test_a_refund_inside_a_batch_reduces_the_net(self) -> None:
        transactions = [
            payment("T1", "10000", "card", fees="250", refunded="4000", settlement_id="STL_1")
        ]
        report = build(MONDAY, transactions, [], [batch("STL_1", MONDAY, "10000", "250", "5750")])
        assert not report.problems, report.problems

    def test_a_quiet_saturday_shows_nothing_landing(self) -> None:
        report = build(date(2024, 5, 18), [], [], [batch("STL_1", MONDAY, "1", "0", "1")])
        assert report.received == []


class TestProblems:
    def test_an_unverified_payment_is_flagged(self) -> None:
        report = build(FRIDAY, [payment("T1", "10000", "card", verified=False)], [])
        kinds = {problem["kind"] for problem in report.problems}
        assert "unverified_payments" in kinds

    def test_cash_is_not_flagged_for_being_unverified(self) -> None:
        """Nothing verifies cash and pretending otherwise is the dishonest bit."""
        report = build(FRIDAY, [payment("T1", "10000", "cash", verified=False)], [])
        assert not report.problems

    def test_a_clean_day_has_no_problems(self) -> None:
        report = build(FRIDAY, [payment("T1", "10000", "card", fees="250")], [])
        assert report.problems == []


class TestMatchingSummary:
    def test_closed_and_queued_are_counted_and_totalled(self) -> None:
        transactions = [payment("T1", "10000", "card"), payment("T2", "40000", "bank_transfer")]
        matches = [
            Match(
                "T1",
                ("INV-1",),
                Layer.EXACT_REFERENCE,
                1.0,
                money_at_risk=Money.from_naira("10000"),
            ),
            Match(
                "T2",
                (),
                Layer.UNRESOLVED,
                0.2,
                needs_human=True,
                money_at_risk=Money.from_naira("40000"),
            ),
        ]
        report = build(FRIDAY, transactions, matches)
        assert report.matched_count == 1
        assert report.queued_count == 1
        assert report.queued_money == Money.from_naira("40000")


def test_the_summary_is_plain_data_a_page_can_render() -> None:
    report = build(FRIDAY, [payment("T1", "10000", "card", fees="250")], [])
    out = summarise(report)
    assert out["day"] == "2024-05-17"
    assert out["taken_today"]["gross"] == "₦10,000.00"
    assert out["balances"] is True
    assert isinstance(out["problems"], list)


@pytest.mark.parametrize("naira", ["0.01", "999999.99", "12345.67"])
def test_no_amount_ever_becomes_a_float(naira: str) -> None:
    report = build(FRIDAY, [payment("T1", naira, "cash")], [])
    assert isinstance(report.gross_taken.kobo, int)
    assert report.gross_taken == Money.from_naira(naira)
