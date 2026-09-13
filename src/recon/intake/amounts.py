"""Reading an amount of money out of a sentence.

Nigerians write and say amounts in a lot of shapes, and a reconciliation tool
that only understands "45000.00" is going to route most of its WhatsApp traffic
to a human for no reason:

    ₦45,000      N45000      45k       45,000 naira
    2.5k         1.2m        N1.5m     forty five thousand naira
    #45,000      45000 NGN   ₦1,250.50

All of them become whole kobo or nothing at all. Three rules it will not bend:

* **"about" is not a number.** "About fifty thousand" has no amount in it. Any
  hedge word next to the figure makes the whole thing unparseable, on purpose.
* **more than one candidate amount is a failure.** "45k for INV-42, balance 15k"
  contains two figures and picking one is guessing.
* **finer than a kobo is a failure**, same as everywhere else in the system.
"""

from __future__ import annotations

import re

from recon.money import Money, MoneyError

#: Words that mean the speaker is not sure. If one of these sits next to the
#: figure, there is no figure.
HEDGES: frozenset[str] = frozenset(
    {"about", "around", "roughly", "approximately", "circa", "maybe", "almost", "nearly", "~"}
)

MULTIPLIERS: dict[str, int] = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}

_UNITS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fourty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES: dict[str, int] = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}

CURRENCY_MARKS = "₦#N"

#: A figure: optional currency mark, digits with optional thousands separators
#: and decimals, optional k/m suffix.
_FIGURE = re.compile(
    r"(?P<mark>[₦#]|\bN(?=\s?\d))?\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,3})?)"
    r"\s*(?P<suffix>k|m)?\b",
    re.IGNORECASE,
)


class AmountError(ValueError):
    """The text had no single, unambiguous amount in it."""


def find_amount(text: str) -> Money:
    """The one amount in this sentence, as whole kobo. Raises if there is not one."""
    lowered = f" {text.lower()} "

    candidates = _digit_amounts(lowered)
    if not candidates:
        candidates = _spelled_out_amounts(lowered)

    if not candidates:
        raise AmountError("no amount in the text")

    distinct = {money.kobo for money, _ in candidates}
    if len(distinct) > 1:
        readable = ", ".join(str(Money(kobo)) for kobo in sorted(distinct))
        raise AmountError(f"more than one amount in the text ({readable})")

    money, position = candidates[0]
    if _hedged(lowered, position):
        raise AmountError('the amount is hedged ("about", "around"), so there is no amount')
    if money.kobo <= 0:
        raise AmountError("the amount is zero or negative")
    return money


def _digit_amounts(lowered: str) -> list[tuple[Money, int]]:
    found: list[tuple[Money, int]] = []
    for match in _FIGURE.finditer(lowered):
        raw = match.group("number").replace(",", "")
        suffix = (match.group("suffix") or "").lower()

        # A bare number with no currency mark, no suffix and no "naira" nearby
        # is probably an invoice number or a date, not money.
        if not suffix and not match.group("mark") and not _currency_nearby(lowered, match.end()):
            continue

        try:
            money = _scale(raw, MULTIPLIERS.get(suffix, 1))
        except (MoneyError, AmountError):
            continue
        found.append((money, match.start()))
    return found


def _scale(raw: str, multiplier: int) -> Money:
    """Apply a k/m suffix without ever going through a float."""
    if multiplier == 1:
        return Money.from_naira(raw)

    whole, _, fraction = raw.partition(".")
    digits = whole + fraction
    shift = multiplier_digits(multiplier) - len(fraction)
    if shift < 0:
        # e.g. "1.2345m" would be finer than a naira in a way that is almost
        # certainly a typo. Refuse rather than round.
        raise AmountError(f"{raw!r} with that suffix is more precise than it looks")
    return Money.from_naira(digits + "0" * shift)


def multiplier_digits(multiplier: int) -> int:
    return len(str(multiplier)) - 1


def _currency_nearby(lowered: str, position: int) -> bool:
    tail = lowered[position : position + 12]
    return "naira" in tail or "ngn" in tail or "kobo" in tail


def _hedged(lowered: str, position: int) -> bool:
    before = lowered[max(0, position - 30) : position]
    return any(hedge in before.split() or hedge in before for hedge in HEDGES)


def _spelled_out_amounts(lowered: str) -> list[tuple[Money, int]]:
    """ "forty five thousand naira" and friends.

    Only trusted when the words are followed by a currency word, because
    "twenty" on its own is usually a quantity of something, not an amount.
    """
    # "forty-five thousand" is one number written with a hyphen in it, so the
    # hyphen has to stop being a token boundary before anything else happens.
    lowered = lowered.replace("-", " ")
    words = re.findall(r"[a-z]+|[^\sa-z]+", lowered)
    positions: list[int] = []
    cursor = 0
    for word in words:
        cursor = lowered.index(word, cursor)
        positions.append(cursor)
        cursor += len(word)

    found: list[tuple[Money, int]] = []
    index = 0
    while index < len(words):
        if words[index] not in _UNITS and words[index] not in _TENS:
            index += 1
            continue
        start = index
        total, current = 0, 0
        while index < len(words):
            word = words[index]
            if word in _UNITS:
                current += _UNITS[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word in _SCALES:
                scale = _SCALES[word]
                if scale == 100:
                    current = max(current, 1) * 100
                else:
                    total += max(current, 1) * scale
                    current = 0
            elif word == "and":
                pass
            else:
                break
            index += 1
        total += current
        if total > 0 and _currency_nearby(lowered, positions[min(index, len(words) - 1)]):
            found.append((Money(total * 100), positions[start]))
    return found
