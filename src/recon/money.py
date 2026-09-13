"""Money, as a whole number of kobo.

One rule: there are no fractions of a kobo anywhere in this system. Not in a
variable, not in a database column, not halfway through a calculation. If a
number cannot be written as a whole number of kobo, this module raises instead
of guessing.

    >>> Money.from_naira("1250.50")
    Money(kobo=125050)
    >>> str(Money.from_naira("1250.50"))
    '₦1,250.50'
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

KOBO_PER_NAIRA: Final = 100

Rounding = Literal["half_up", "down", "up"]

#  optional sign, naira digits, optional ".dd"
_NAIRA_PATTERN: Final = re.compile(r"^(?P<sign>[-+]?)(?P<naira>\d+)(?:\.(?P<kobo>\d+))?$")


class MoneyError(ValueError):
    """Something was wrong with an amount. We never swallow these."""


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """An exact amount of Nigerian naira, held as a whole number of kobo."""

    kobo: int

    def __post_init__(self) -> None:
        # bool is a subclass of int and would sneak through an isinstance check.
        if isinstance(self.kobo, bool) or not isinstance(self.kobo, int):
            raise MoneyError(
                f"kobo must be an int, got {type(self.kobo).__name__}: {self.kobo!r}. "
                "A float here means someone divided money somewhere."
            )

    # ---------------------------------------------------------------- builders

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def from_kobo(cls, kobo: int) -> Money:
        """Paystack sends NGN amounts in kobo already, so this is the usual door in."""
        return cls(kobo)

    @classmethod
    def from_naira(cls, amount: str | int) -> Money:
        """Parse naira written as text, e.g. "1250.50", "1250", "-40.05".

        Parsing is done on the characters, never through float(), so "0.07"
        cannot drift. More than two decimal places is an error, not a rounding
        opportunity: we refuse to decide on the business's behalf.
        """
        if isinstance(amount, bool | float):
            raise MoneyError(
                f"refusing to build Money from a float ({amount!r}). "
                'Pass a string instead, e.g. Money.from_naira("1250.50").'
            )
        text = str(amount).strip().replace(",", "").replace("_", "")
        match = _NAIRA_PATTERN.match(text)
        if match is None:
            raise MoneyError(f"not a naira amount: {amount!r}")

        fraction = match.group("kobo") or ""
        if len(fraction) > 2:
            raise MoneyError(
                f"{amount!r} is finer than one kobo. "
                "Round it yourself and say which way, or keep the exact value."
            )

        naira = int(match.group("naira"))
        kobo = int(fraction.ljust(2, "0") or "0")
        total = naira * KOBO_PER_NAIRA + kobo
        return cls(-total if match.group("sign") == "-" else total)

    # ------------------------------------------------------------- arithmetic

    def __add__(self, other: Money) -> Money:
        return Money(self.kobo + _as_money(other).kobo)

    def __sub__(self, other: Money) -> Money:
        return Money(self.kobo - _as_money(other).kobo)

    def __neg__(self) -> Money:
        return Money(-self.kobo)

    def __abs__(self) -> Money:
        return Money(abs(self.kobo))

    def __mul__(self, count: int) -> Money:
        """Multiply by a whole number of times. Three copies of ₦50 is ₦150."""
        if isinstance(count, bool) or not isinstance(count, int):
            raise MoneyError(
                f"can only multiply Money by a whole number, got {count!r}. "
                "For a percentage use .percent_bps()."
            )
        return Money(self.kobo * count)

    __rmul__ = __mul__

    def percent_bps(self, bps: int, rounding: Rounding = "half_up") -> Money:
        """Take a percentage, written in basis points. 1.5% is 150 bps.

        Paystack's card fee is 1.5% + ₦100, so this exists to compute fees
        without ever touching a float. You must say how to round, because a
        half-kobo has to land somewhere and that somewhere is a business
        decision.
        """
        if isinstance(bps, bool) or not isinstance(bps, int):
            raise MoneyError(f"bps must be a whole number, got {bps!r}")
        return Money(_divide(self.kobo * bps, 10_000, rounding))

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split this amount into parts, losing nothing.

        Splitting ₦10 three ways gives 334 + 333 + 333 kobo, not 333.33 each.
        The leftover kobo go to the earliest parts (the "largest remainder"
        rule). The parts always add back up to the original exactly.
        """
        if not weights:
            raise MoneyError("cannot allocate across zero parts")
        if any(isinstance(w, bool) or not isinstance(w, int) or w < 0 for w in weights):
            raise MoneyError(f"weights must be non-negative whole numbers, got {weights!r}")
        total_weight = sum(weights)
        if total_weight == 0:
            raise MoneyError("weights must not all be zero")

        parts = [Money(self.kobo * w // total_weight) for w in weights]
        remainder = self.kobo - sum(p.kobo for p in parts)
        step = 1 if remainder >= 0 else -1
        for i in range(abs(remainder)):
            parts[i % len(parts)] = parts[i % len(parts)] + Money(step)
        return parts

    # ------------------------------------------------------------------ views

    @property
    def naira_part(self) -> int:
        """The whole naira, rounded toward zero. -₦1.50 gives -1."""
        return abs(self.kobo) // KOBO_PER_NAIRA * (-1 if self.kobo < 0 else 1)

    @property
    def kobo_part(self) -> int:
        """The kobo left over after the whole naira, always 0..99."""
        return abs(self.kobo) % KOBO_PER_NAIRA

    def is_zero(self) -> bool:
        return self.kobo == 0

    def to_naira_str(self) -> str:
        """Plain decimal text, no symbol, no commas. Good for CSV and JSON."""
        sign = "-" if self.kobo < 0 else ""
        return f"{sign}{abs(self.kobo) // KOBO_PER_NAIRA}.{abs(self.kobo) % KOBO_PER_NAIRA:02d}"

    def __str__(self) -> str:
        """What a human reads: ₦1,250.50."""
        sign = "-" if self.kobo < 0 else ""
        whole = abs(self.kobo) // KOBO_PER_NAIRA
        return f"{sign}₦{whole:,}.{abs(self.kobo) % KOBO_PER_NAIRA:02d}"


def _as_money(value: Money) -> Money:
    if not isinstance(value, Money):
        raise MoneyError(
            f"expected Money, got {type(value).__name__}: {value!r}. Wrap it: Money.from_kobo(...)."
        )
    return value


def _divide(numerator: int, denominator: int, rounding: Rounding) -> int:
    """Integer division with a rounding rule you had to name out loud."""
    if denominator == 0:
        raise MoneyError("division by zero in a money calculation")
    # Check the rule before doing the sums. Otherwise a typo'd rule slips
    # through unnoticed whenever the division happens to come out exact.
    if rounding not in ("half_up", "down", "up"):
        raise MoneyError(f"unknown rounding rule: {rounding!r}")

    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    n, d = abs(numerator), abs(denominator)
    whole, rest = divmod(n, d)
    if rest == 0 or rounding == "down":  # "down" means toward zero
        return sign * whole
    if rounding == "up":  # away from zero
        return sign * (whole + 1)
    return sign * (whole + 1 if rest * 2 >= d else whole)


def sum_money(amounts: list[Money]) -> Money:
    """Add up a list. Exists so nobody reaches for builtin sum() and gets an int 0."""
    total = 0
    for amount in amounts:
        total += _as_money(amount).kobo
    return Money(total)
