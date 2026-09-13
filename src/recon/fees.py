"""What Paystack keeps, and when the rest of it lands in the bank.

These are Paystack's published Nigerian test-mode rates. They are in one file
with one test suite because a fee model that is 3 kobo out is a reconciliation
that never closes, and because the rates change: when they do, this is the only
file to edit.

Rates as modelled:

* **Card** — 1.5% plus ₦100. The ₦100 is waived on anything under ₦2,500. The
  whole fee is capped at ₦2,000, so above about ₦126,667 the fee stops growing.
* **Transfer into a dedicated virtual account** — 1%, capped at ₦300.
* **Main-account transfer** — the bank's charge, not Paystack's. We model it as
  zero and say so, because we genuinely do not see it.
* **Cash** — no fee. A human collected it.

Settlement timing is T+1 working day for cards. Money taken on Friday lands on
Monday, and that gap is most of why yesterday's takings never match today's
bank balance.
"""

from __future__ import annotations

from datetime import date, timedelta

from recon.enums import Channel
from recon.money import Money

CARD_RATE_BPS = 150
"""1.5%, in basis points."""

CARD_FLAT_FEE = Money.from_naira("100")
CARD_FLAT_FEE_WAIVED_BELOW = Money.from_naira("2500")
CARD_FEE_CAP = Money.from_naira("2000")

DVA_RATE_BPS = 100
"""1%, in basis points."""

DVA_FEE_CAP = Money.from_naira("300")


def fee_for(channel: Channel, amount: Money) -> Money:
    """What Paystack keeps out of one payment.

    Rounds half up, so a half-kobo goes to Paystack. That is the direction their
    own numbers round, and guessing the other way means every settlement is a
    kobo short and nobody can tell you why.
    """
    if amount.kobo <= 0:
        return Money.zero()

    if channel is Channel.CARD:
        fee = amount.percent_bps(CARD_RATE_BPS, rounding="half_up")
        if amount >= CARD_FLAT_FEE_WAIVED_BELOW:
            fee = fee + CARD_FLAT_FEE
        return min(fee, CARD_FEE_CAP)

    if channel is Channel.DVA:
        return min(amount.percent_bps(DVA_RATE_BPS, rounding="half_up"), DVA_FEE_CAP)

    # A transfer into the main account is charged by the customer's own bank, to
    # the customer. We never see it, so we do not invent it.
    return Money.zero()


def net_of_fee(channel: Channel, amount: Money) -> Money:
    """What actually reaches the bank account."""
    return amount - fee_for(channel, amount)


def settlement_date(paid_on: date, channel: Channel) -> date:
    """When money taken on `paid_on` lands in the bank.

    Cash is already in the till, so it settles the same day. Everything else is
    the next working day, which is why Friday, Saturday and Sunday all land on
    Monday.
    """
    if channel is Channel.CASH:
        return paid_on
    settles = paid_on + timedelta(days=1)
    while settles.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        settles += timedelta(days=1)
    return settles
