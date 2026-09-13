"""Nigerian names, and the ways they get written down wrong.

Matching a transfer to a customer usually comes down to comparing the name on
the transfer with the name on the invoice, and those two names are very often
not the same string. The reasons are boring and completely predictable:

* Banks print names last-name-first, in capitals. "Ada Okonkwo" becomes
  "OKONKWO ADA".
* The same name has several accepted spellings. Mohammed, Muhammad, Muhammed
  and Mohamed are one name. So are Aisha, Aishat and Aishah.
* People are known by their second name. Oluwaseun Adeyemi sends money as Seun.
* Someone typing at a counter drops a letter, doubles a letter, or swaps two.
* Tone marks and apostrophes survive in one system and not the other.

This module holds the name pool and the mangling, so that the generated corpus
fails in the ways the real world fails and not in invented ways.
"""

from __future__ import annotations

import random
import unicodedata

FIRST_NAMES: tuple[str, ...] = (
    "Adaeze",
    "Chinedu",
    "Oluwaseun",
    "Ngozi",
    "Ibrahim",
    "Aisha",
    "Emeka",
    "Folake",
    "Yusuf",
    "Chiamaka",
    "Babatunde",
    "Halima",
    "Obinna",
    "Temitope",
    "Musa",
    "Blessing",
    "Kelechi",
    "Olumide",
    "Zainab",
    "Uchenna",
    "Damilola",
    "Abubakar",
    "Chidinma",
    "Segun",
    "Amina",
    "Ikechukwu",
    "Bolanle",
    "Sadiq",
    "Nnamdi",
    "Funmilayo",
    "Tobiloba",
    "Hauwa",
    "Chukwuemeka",
    "Adebayo",
    "Rukayat",
    "Ifeanyi",
    "Oluwatobi",
    "Maryam",
    "Ekene",
    "Abiodun",
)

LAST_NAMES: tuple[str, ...] = (
    "Okonkwo",
    "Adeyemi",
    "Bello",
    "Eze",
    "Okafor",
    "Balogun",
    "Nwosu",
    "Abubakar",
    "Oyelaran",
    "Chukwu",
    "Adewale",
    "Danjuma",
    "Obi",
    "Ogundipe",
    "Suleiman",
    "Nwachukwu",
    "Akintola",
    "Musa",
    "Onyeka",
    "Lawal",
    "Ezenwa",
    "Ogunleye",
    "Ibrahim",
    "Anyanwu",
    "Sanusi",
    "Afolabi",
    "Udeh",
    "Mohammed",
    "Olaniyi",
    "Uche",
)

#: Real, commonly accepted alternative spellings. Not typos.
SPELLING_VARIANTS: dict[str, tuple[str, ...]] = {
    "Mohammed": ("Muhammad", "Muhammed", "Mohamed", "Mohammad"),
    "Ibrahim": ("Ibraheem", "Ibrahaim"),
    "Aisha": ("Aishat", "Aishah", "Ayesha"),
    "Abubakar": ("Abubakr", "Aboubakar", "Abu Bakar"),
    "Oluwaseun": ("Oluwashaun", "Oluwaseun", "Olusheun"),
    "Chukwuemeka": ("Chukwuemeke", "Chuwuemeka"),
    "Chinedu": ("Chinedum", "Chinedo"),
    "Okonkwo": ("Okonko", "Okonkwu"),
    "Adeyemi": ("Adeyemy", "Adeyemi"),
    "Nwachukwu": ("Nwachuku", "Nwachukwo"),
    "Balogun": ("Balogun", "Balogon"),
    "Suleiman": ("Sulaiman", "Suleman", "Sulieman"),
    "Temitope": ("Temitopi", "Temytope"),
    "Ngozi": ("Ngozichukwu", "Ngozy"),
    "Folake": ("Folakemi", "Pholake"),
    "Damilola": ("Damilolah", "Dammy"),
    "Emeka": ("Emeca", "Emekah"),
    "Halima": ("Haleema", "Halimah"),
    "Zainab": ("Zeinab", "Zainabu"),
    "Blessing": ("Blesing", "Blessin"),
}

#: People answer to the back half of a long first name.
SHORT_FORMS: dict[str, str] = {
    "Oluwaseun": "Seun",
    "Oluwatobi": "Tobi",
    "Chukwuemeka": "Emeka",
    "Babatunde": "Tunde",
    "Olumide": "Mide",
    "Temitope": "Tope",
    "Damilola": "Dami",
    "Funmilayo": "Funmi",
    "Ikechukwu": "Ike",
    "Adebayo": "Bayo",
    "Tobiloba": "Toba",
    "Abiodun": "Biodun",
    "Chidinma": "Chidi",
    "Uchenna": "Uche",
}


def full_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def normalise(name: str) -> str:
    """Strip accents, punctuation and case. Used by the matcher, not the generator."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in stripped)
    return " ".join(cleaned.lower().split())


def name_tokens(name: str) -> list[str]:
    return normalise(name).split()


# --------------------------------------------------------------- mangling


def reorder(name: str) -> str:
    """What the bank prints: surname first, shouted."""
    parts = name.split()
    if len(parts) < 2:
        return name.upper()
    return " ".join([parts[-1], *parts[:-1]]).upper()


def alternative_spelling(name: str, rng: random.Random) -> str:
    """Swap one part for a genuinely different accepted spelling."""
    parts = name.split()
    options = [i for i, p in enumerate(parts) if p in SPELLING_VARIANTS]
    if not options:
        return name
    index = rng.choice(options)
    parts[index] = rng.choice(SPELLING_VARIANTS[parts[index]])
    return " ".join(parts)


def short_form(name: str, rng: random.Random) -> str:
    """Use the name they actually go by."""
    parts = name.split()
    if parts and parts[0] in SHORT_FORMS:
        parts[0] = SHORT_FORMS[parts[0]]
    elif len(parts) >= 2:
        parts[0] = f"{parts[0][0]}."
    return " ".join(parts)


def typo(name: str, rng: random.Random) -> str:
    """One slip of the finger: a dropped, doubled or swapped letter."""
    parts = name.split()
    index = rng.randrange(len(parts))
    word = parts[index]
    if len(word) < 4:
        return name

    position = rng.randrange(1, len(word) - 1)
    kind = rng.choice(("drop", "double", "swap"))
    if kind == "drop":
        word = word[:position] + word[position + 1 :]
    elif kind == "double":
        word = word[:position] + word[position] * 2 + word[position:]
    else:
        word = word[:position] + word[position + 1] + word[position] + word[position + 2 :]

    parts[index] = word
    return " ".join(parts)


def initials_only(name: str) -> str:
    parts = name.split()
    if len(parts) < 2:
        return name
    return f"{parts[0][0]}. {parts[-1]}"


#: How likely each kind of mangling is. Roughly what a week of real narrations
#: looks like: most names arrive readable, a stubborn minority do not.
MANGLERS: tuple[tuple[str, float], ...] = (
    ("clean", 0.34),
    ("reorder", 0.24),
    ("alternative_spelling", 0.14),
    ("short_form", 0.10),
    ("typo", 0.10),
    ("initials", 0.05),
    ("reorder_and_typo", 0.03),
)


def mangle(name: str, rng: random.Random) -> tuple[str, str]:
    """Return (the name as it will appear, which mangling was applied).

    The label is kept so the evaluation harness can report accuracy per kind of
    mess, instead of one average that hides the hard cases.
    """
    kinds = [k for k, _ in MANGLERS]
    weights = [w for _, w in MANGLERS]
    kind = rng.choices(kinds, weights=weights, k=1)[0]

    if kind == "clean":
        return name.upper(), kind
    if kind == "reorder":
        return reorder(name), kind
    if kind == "alternative_spelling":
        return alternative_spelling(name, rng).upper(), kind
    if kind == "short_form":
        return short_form(name, rng).upper(), kind
    if kind == "typo":
        return typo(name, rng).upper(), kind
    if kind == "initials":
        return initials_only(name).upper(), kind
    return reorder(typo(name, rng)), "reorder_and_typo"
