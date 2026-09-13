"""How alike are two names.

The hard part of matching a Nigerian bank transfer is that the name on the
transfer and the name on the invoice are the same person written two different
ways. So this returns a number between 0 and 1, and it has to survive:

* reordering — "OKONKWO ADA" is "Ada Okonkwo"
* accepted alternative spellings — "Muhammad" is "Mohammed"
* short forms — "Seun Adeyemi" is "Oluwaseun Adeyemi"
* one dropped or doubled letter — "Okonko" is "Okonkwo"
* a surrounding narration — "NIP/GTB/ADA OKONKWO/PAYMENT" contains the name

And it must NOT survive two different people with similar names. "Chinedu
Okafor" and "Chinedum Okafor" are two customers in the corpus on purpose, and a
similarity function that calls them the same is worse than useless, because it
is confidently wrong about whose money this is.

No third-party library: `difflib` is in the standard library and is good enough
once the strings have been normalised properly, which is most of the work
anyway.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache

from recon.corpus.names import SHORT_FORMS, SPELLING_VARIANTS, normalise

#: Words that turn up in narrations and are not part of anybody's name.
NOISE_WORDS: frozenset[str] = frozenset(
    {
        "trf",
        "transfer",
        "from",
        "to",
        "nip",
        "web",
        "mobile",
        "ussd",
        "payment",
        "payments",
        "for",
        "goods",
        "balance",
        "part",
        "invoice",
        "order",
        "supply",
        "settlement",
        "ltd",
        "limited",
        "stores",
        "store",
        "the",
        "and",
        "cash",
        "received",
        "gtb",
        "uba",
        "zenith",
        "firstbank",
        "access",
        "kuda",
        "opay",
        "moniepoint",
        "sterling",
        "fcmb",
        "wema",
        "palmpay",
        "polaris",
        "bank",
    }
)


@lru_cache(maxsize=4096)
def _variant_key(token: str) -> str:
    """Fold a token onto one representative spelling.

    Built from the same variant table the corpus generator uses, read in both
    directions, so "muhammad" and "mohammed" land on the same key. This is the
    one place where knowing something about Nigerian names beats generic string
    distance.
    """
    for canonical, variants in SPELLING_VARIANTS.items():
        if token == canonical.lower() or token in (v.lower() for v in variants):
            return canonical.lower()
    for long_form, short in SHORT_FORMS.items():
        if token == short.lower():
            return long_form.lower()
    return token


def meaningful_tokens(text: str) -> frozenset[str]:
    """The parts of a string that could plausibly be someone's name.

    Spelling is left exactly as written. Folding happens in `folded_tokens`,
    separately, so the two can be weighed against each other.
    """
    tokens = normalise(text).split()
    return frozenset(
        token
        for token in tokens
        if len(token) > 1 and token not in NOISE_WORDS and not token.isdigit()
    )


def folded_tokens(text: str) -> frozenset[str]:
    """The same words, with accepted alternative spellings folded together."""
    return frozenset(_variant_key(token) for token in meaningful_tokens(text))


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """What share of the shorter name's words appear in the longer one.

    Deliberately not Jaccard. The left side is often a whole narration with a
    bank name and a purpose in it, and Jaccard would punish that heavily for
    words that were never going to match.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def token_overlap(left: str, right: str) -> float:
    """Word overlap, spelling as written."""
    return _overlap(meaningful_tokens(left), meaningful_tokens(right))


def folded_overlap(left: str, right: str) -> float:
    """Word overlap once Muhammad and Mohammed count as the same word."""
    return _overlap(folded_tokens(left), folded_tokens(right))


def best_token_pair_ratio(left: str, right: str) -> float:
    """For each word in the shorter name, how close is its best partner.

    This is what catches a single dropped letter: "okonko" against "okonkwo"
    scores about 0.92 rather than 0. Uses the spelling as written, so a missing
    letter still costs something.
    """
    a, b = meaningful_tokens(left), meaningful_tokens(right)
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    scores = [max(_ratio(token, other) for other in longer) for token in shorter]
    return sum(scores) / len(scores)


def sequence_ratio(left: str, right: str) -> float:
    """Whole-string similarity after sorting the words. Handles reordering."""
    a = " ".join(sorted(meaningful_tokens(left)))
    b = " ".join(sorted(meaningful_tokens(right)))
    if not a or not b:
        return 0.0
    return _ratio(a, b)


@lru_cache(maxsize=100_000)
def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def name_similarity(observed: str, customer_name: str) -> float:
    """One number for "is this the same person".

    `observed` may be a bare name or a whole narration.

    Four views, blended rather than maximised. The blend matters more than the
    exact weights, and the reason is the pair of customers called Chinedu Okafor
    and Chinedum Okafor. Fold their spellings together and both score a perfect
    1.0 against the same transfer, which is not "very similar", it is "this
    function cannot tell two of your customers apart". So the strict,
    spelling-as-written views carry most of the weight and the forgiving ones
    only top it up. A variant spelling still scores high; an exact spelling
    always scores higher.
    """
    if not observed.strip() or not customer_name.strip():
        return 0.0

    exact = token_overlap(observed, customer_name)
    folded = folded_overlap(observed, customer_name)
    per_token = best_token_pair_ratio(observed, customer_name)
    whole = sequence_ratio(observed, customer_name)

    score = 0.40 * exact + 0.20 * folded + 0.25 * per_token + 0.15 * whole
    return max(0.0, min(1.0, score))


def surname_matches(observed: str, customer_name: str) -> bool:
    """Does the customer's surname appear, spelled exactly?

    A weak signal on its own — plenty of people share a surname — but a useful
    one next to a first name that nearly matches.
    """
    parts = normalise(customer_name).split()
    if not parts:
        return False
    return parts[-1] in meaningful_tokens(observed)
