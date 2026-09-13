"""The strings Nigerian banks put in the narration field.

When money lands in the business's main account, this string is everything we
get. No customer id, no order number, no email. Just whatever the sending bank
chose to print, which is a different format for every bank and sometimes a
different format for the same bank depending on whether the transfer came from
the app, USSD or a branch.

Templates below are modelled on what actually shows up on a GTBank or Zenith
statement. The `{name}` slot gets a mangled name from `names.mangle`, which is
where most of the matching difficulty comes from.
"""

from __future__ import annotations

import random

BANKS: tuple[str, ...] = (
    "GTB",
    "UBA",
    "ZENITH",
    "FIRSTBANK",
    "ACCESS",
    "KUDA",
    "OPAY",
    "MONIEPOINT",
    "STERLING",
    "FCMB",
    "WEMA",
    "PALMPAY",
    "POLARIS",
)

PURPOSES: tuple[str, ...] = (
    "PAYMENT FOR GOODS",
    "PAYMENT",
    "GOODS",
    "BALANCE",
    "PART PAYMENT",
    "INVOICE PAYMENT",
    "FOR ORDER",
    "SUPPLY",
    "SETTLEMENT",
    "TRANSFER",
)

#: Narrations that carry no order reference at all. The common case.
NAME_ONLY_TEMPLATES: tuple[str, ...] = (
    "TRF FROM {name}",
    "NIP/{bank}/{name}/{purpose}",
    "{session}/{name}/TRANSFER",
    "Mobile Transfer from {name}",
    "{name} TO STACK STORES LTD",
    "TRF/{name}/{purpose}",
    "USSD TRF FROM {name}",
    "{name}",
    "{purpose} - {name}",
    "{bank} TRANSFER {name}",
    "WEB TRF {name} {purpose}",
)

#: Narrations where the payer did type the order reference in. The easy case,
#: and the one the deterministic layer is built to catch.
WITH_REFERENCE_TEMPLATES: tuple[str, ...] = (
    "TRF FROM {name} {order_ref}",
    "NIP/{bank}/{name}/{order_ref}",
    "PAYMENT {order_ref}",
    "{order_ref} {name}",
    "TRF/{name}/PAYMENT FOR {order_ref}",
    "{purpose} {order_ref}",
)


def session_id(rng: random.Random) -> str:
    """The 12-to-30 digit NIP session id that fronts a lot of narrations."""
    return "".join(str(rng.randrange(10)) for _ in range(rng.choice((12, 18, 24))))


def narration(
    rng: random.Random, payer_name: str, order_ref: str | None = None
) -> tuple[str, bool]:
    """Build one narration.

    Returns the string and whether it contains the order reference, so the
    corpus can record which cases were winnable by the deterministic layer.
    """
    if order_ref is not None:
        template = rng.choice(WITH_REFERENCE_TEMPLATES)
        carries_reference = True
    else:
        template = rng.choice(NAME_ONLY_TEMPLATES)
        carries_reference = False

    text = template.format(
        name=payer_name,
        bank=rng.choice(BANKS),
        purpose=rng.choice(PURPOSES),
        session=session_id(rng),
        order_ref=order_ref or "",
    )
    return " ".join(text.split()), carries_reference
