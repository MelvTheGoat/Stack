from __future__ import annotations

from datetime import date

import pytest

from recon.enums import Channel
from recon.fees import (
    CARD_FEE_CAP,
    fee_for,
    net_of_fee,
    settlement_date,
)
from recon.money import Money


class TestCardFees:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("1000", "15.00"),  # under 2,500 so the flat 100 is waived
            ("2499.99", "37.50"),  # 1.5% of 249999 kobo = 3749.985 -> 3750
            ("2500", "137.50"),  # the flat fee switches on here
            ("10000", "250.00"),
            ("100000", "1600.00"),
            ("126666.67", "2000.00"),  # the cap bites right about here
            ("500000", "2000.00"),  # and never grows again
            ("0.01", "0.00"),  # 1.5% of one kobo rounds to nothing
            ("1", "0.02"),  # 1.5 kobo, rounded half up
        ],
    )
    def test_the_published_rate(self, amount: str, expected: str) -> None:
        assert fee_for(Channel.CARD, Money.from_naira(amount)) == Money.from_naira(expected)

    def test_the_cap_is_never_exceeded(self) -> None:
        for naira in (1, 1_000, 126_000, 127_000, 10_000_000):
            assert fee_for(Channel.CARD, Money.from_naira(str(naira))) <= CARD_FEE_CAP


class TestTransferFees:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [("1000", "10.00"), ("30000", "300.00"), ("50000", "300.00"), ("100", "1.00")],
    )
    def test_dva_is_one_percent_capped_at_300(self, amount: str, expected: str) -> None:
        assert fee_for(Channel.DVA, Money.from_naira(amount)) == Money.from_naira(expected)

    def test_a_main_account_transfer_costs_us_nothing_we_can_see(self) -> None:
        assert fee_for(Channel.BANK_TRANSFER, Money.from_naira("50000")).is_zero()

    def test_cash_has_no_fee(self) -> None:
        assert fee_for(Channel.CASH, Money.from_naira("50000")).is_zero()


class TestEdges:
    @pytest.mark.parametrize("channel", list(Channel))
    def test_a_zero_or_negative_amount_has_no_fee(self, channel: Channel) -> None:
        assert fee_for(channel, Money.zero()).is_zero()
        assert fee_for(channel, Money(-1000)).is_zero()

    @pytest.mark.parametrize("channel", list(Channel))
    def test_net_plus_fee_is_always_the_gross(self, channel: Channel) -> None:
        for kobo in (1, 99, 250_000, 12_666_667, 100_000_000):
            amount = Money(kobo)
            assert net_of_fee(channel, amount) + fee_for(channel, amount) == amount


class TestSettlementTiming:
    def test_a_tuesday_card_payment_lands_on_wednesday(self) -> None:
        assert settlement_date(date(2024, 5, 14), Channel.CARD) == date(2024, 5, 15)

    @pytest.mark.parametrize("day", [date(2024, 5, 17), date(2024, 5, 18), date(2024, 5, 19)])
    def test_friday_saturday_and_sunday_all_land_on_monday(self, day: date) -> None:
        assert settlement_date(day, Channel.CARD) == date(2024, 5, 20)

    def test_cash_settles_the_same_day_because_it_is_already_here(self) -> None:
        assert settlement_date(date(2024, 5, 18), Channel.CASH) == date(2024, 5, 18)
