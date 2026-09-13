"""Tests for the money type. If these pass, no amount in the system can drift."""

from __future__ import annotations

import pytest

from recon.money import Money, MoneyError, sum_money


class TestTheHundredBoundary:
    """The x100 boundary is where money bugs live. Poke every side of it."""

    @pytest.mark.parametrize(
        ("text", "kobo"),
        [
            ("0", 0),
            ("0.00", 0),
            ("0.01", 1),
            ("0.1", 10),  # one decimal place means tenths of naira, so 10 kobo
            ("0.99", 99),
            ("1", 100),
            ("1.00", 100),
            ("0.995", None),  # finer than a kobo -> error, not a round
            ("1250.50", 125050),
            ("-40.05", -4005),
            ("+7.07", 707),
            ("1,250.50", 125050),  # commas are tolerated on the way in
            ("  12.34  ", 1234),
            ("99999999.99", 9999999999),
        ],
    )
    def test_parsing_naira_text(self, text: str, kobo: int | None) -> None:
        if kobo is None:
            with pytest.raises(MoneyError, match="finer than one kobo"):
                Money.from_naira(text)
        else:
            assert Money.from_naira(text).kobo == kobo

    def test_a_tenth_of_a_naira_is_ten_kobo_exactly(self) -> None:
        # The classic float failure: 0.1 + 0.2 != 0.3. Here it must.
        tenth = Money.from_naira("0.10")
        fifth = Money.from_naira("0.20")
        assert tenth + fifth == Money.from_naira("0.30")

    def test_a_thousand_seven_kobo_payments_add_up_exactly(self) -> None:
        total = sum_money([Money.from_naira("0.07")] * 1000)
        assert total.kobo == 7000
        assert str(total) == "₦70.00"


class TestRefusingBadInput:
    """Money errors are loud. None of these may pass silently."""

    def test_float_kobo_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="must be an int"):
            Money(1250.5)  # type: ignore[arg-type]

    def test_float_naira_is_rejected_with_a_hint(self) -> None:
        with pytest.raises(MoneyError, match="refusing to build Money from a float"):
            Money.from_naira(1250.50)  # type: ignore[arg-type]

    def test_bool_is_not_a_number_of_kobo(self) -> None:
        with pytest.raises(MoneyError, match="must be an int"):
            Money(True)

    @pytest.mark.parametrize("junk", ["", "abc", "₦100", "1.2.3", "1e3", "--5", "1 000"])
    def test_junk_text_is_rejected(self, junk: str) -> None:
        with pytest.raises(MoneyError, match="not a naira amount"):
            Money.from_naira(junk)

    def test_adding_a_bare_int_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="expected Money"):
            Money.from_kobo(100) + 100  # type: ignore[operator]

    def test_multiplying_by_a_fraction_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="whole number"):
            Money.from_naira("100") * 0.015  # type: ignore[operator]


class TestRounding:
    """Percentages have to round somewhere. You must say where."""

    @pytest.mark.parametrize(
        ("kobo", "bps", "rule", "expected"),
        [
            (100_000, 150, "half_up", 1500),  # ₦1000 at 1.5% = ₦15.00
            (333, 150, "half_up", 5),  # 4.995 kobo -> 5
            (333, 150, "down", 4),
            (333, 150, "up", 5),
            (1000, 50, "half_up", 5),  # exactly 5, no rounding needed
            (100, 150, "half_up", 2),  # 1.5 kobo -> 2 (half goes up)
            (100, 150, "down", 1),
            (-100, 150, "half_up", -2),  # away from zero, symmetric with positive
            (-100, 150, "down", -1),
        ],
    )
    def test_percent_bps(self, kobo: int, bps: int, rule: str, expected: int) -> None:
        assert Money(kobo).percent_bps(bps, rounding=rule).kobo == expected  # type: ignore[arg-type]

    def test_unknown_rounding_rule_is_loud(self) -> None:
        with pytest.raises(MoneyError, match="unknown rounding rule"):
            Money(1000).percent_bps(150, rounding="banker")  # type: ignore[arg-type]

    def test_paystack_card_fee_shape(self) -> None:
        """1.5% + ₦100, waived under ₦2,500, capped at ₦2,000. Checked here only
        as arithmetic; the real policy lives in the fee model."""
        amount = Money.from_naira("10000")
        fee = amount.percent_bps(150) + Money.from_naira("100")
        assert str(fee) == "₦250.00"


class TestAllocate:
    """Splitting one payment across several invoices must not lose a kobo."""

    def test_ten_naira_three_ways(self) -> None:
        parts = Money.from_naira("10").allocate([1, 1, 1])
        assert [p.kobo for p in parts] == [334, 333, 333]
        assert sum_money(parts) == Money.from_naira("10")

    def test_weighted_split_matches_invoice_sizes(self) -> None:
        payment = Money.from_naira("1000")
        parts = payment.allocate([50_000, 30_000, 20_000])
        assert [str(p) for p in parts] == ["₦500.00", "₦300.00", "₦200.00"]
        assert sum_money(parts) == payment

    @pytest.mark.parametrize("weights", [[1, 1, 1], [7, 11, 13], [1], [1, 0, 2]])
    @pytest.mark.parametrize("kobo", [1, 2, 99, 100, 101, 12_345, 999_999])
    def test_parts_always_add_back_to_the_whole(self, kobo: int, weights: list[int]) -> None:
        parts = Money(kobo).allocate(weights)
        assert sum_money(parts) == Money(kobo)
        assert len(parts) == len(weights)

    def test_negative_amount_splits_without_loss(self) -> None:
        parts = Money(-1000).allocate([1, 1, 1])
        assert sum_money(parts) == Money(-1000)

    def test_bad_weights_are_loud(self) -> None:
        with pytest.raises(MoneyError, match="zero parts"):
            Money(100).allocate([])
        with pytest.raises(MoneyError, match="must not all be zero"):
            Money(100).allocate([0, 0])
        with pytest.raises(MoneyError, match="non-negative"):
            Money(100).allocate([-1, 2])


class TestFormatting:
    @pytest.mark.parametrize(
        ("kobo", "human", "machine"),
        [
            (0, "₦0.00", "0.00"),
            (5, "₦0.05", "0.05"),
            (50, "₦0.50", "0.50"),
            (100, "₦1.00", "1.00"),
            (125050, "₦1,250.50", "1250.50"),
            (100_000_000, "₦1,000,000.00", "1000000.00"),
            (-4005, "-₦40.05", "-40.05"),
            (-5, "-₦0.05", "-0.05"),
        ],
    )
    def test_both_renderings(self, kobo: int, human: str, machine: str) -> None:
        assert str(Money(kobo)) == human
        assert Money(kobo).to_naira_str() == machine

    def test_machine_text_round_trips(self) -> None:
        for kobo in (0, 1, 99, 100, 125050, -4005, 9_999_999_999):
            assert Money.from_naira(Money(kobo).to_naira_str()).kobo == kobo

    def test_parts(self) -> None:
        assert Money(125050).naira_part == 1250
        assert Money(125050).kobo_part == 50
        assert Money(-150).naira_part == -1
        assert Money(-150).kobo_part == 50


class TestBehavesLikeAValue:
    def test_equality_and_ordering(self) -> None:
        assert Money(100) == Money(100)
        assert Money(100) != Money(101)
        assert Money(100) < Money(101)
        assert max(Money(1), Money(500), Money(3)) == Money(500)

    def test_hashable_so_it_can_key_a_dict(self) -> None:
        assert {Money(100): "a"}[Money(100)] == "a"

    def test_frozen(self) -> None:
        with pytest.raises(Exception, match=r"(?i)(frozen|cannot assign)"):
            Money(100).kobo = 200  # type: ignore[misc]

    def test_arithmetic(self) -> None:
        assert Money(100) + Money(50) == Money(150)
        assert Money(100) - Money(150) == Money(-50)
        assert -Money(100) == Money(-100)
        assert abs(Money(-100)) == Money(100)
        assert Money(100) * 3 == Money(300)
        assert 3 * Money(100) == Money(300)
        assert Money.zero().is_zero()

    def test_sum_money_of_empty_list_is_zero_money_not_int(self) -> None:
        assert sum_money([]) == Money.zero()

    def test_sum_money_rejects_a_stray_int(self) -> None:
        with pytest.raises(MoneyError, match="expected Money"):
            sum_money([Money(1), 2])  # type: ignore[list-item]
