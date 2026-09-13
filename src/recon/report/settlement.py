"""The daily report: what came in, what Paystack kept, and what reached the bank.

The thing that confuses everybody about a Paystack account is that yesterday's
sales and today's bank credit are two different numbers, and neither is wrong.
Three separate reasons pull them apart, and this report shows all three rather
than netting them off into one unexplained difference:

1. **Fees.** Paystack keeps 1.5% + ₦100 on a card payment. The customer paid
   ₦10,000; ₦9,750 arrives.
2. **Timing.** Card and dedicated-account money settles on the next working day.
   Friday's takings arrive on Monday, so a Monday statement shows three days of
   sales at once and Saturday's shows nothing.
3. **Channel.** Cash never settles, because it is already in the till. A
   transfer into the main account never settles either, because it went straight
   to the bank and Paystack was never involved. Only card and dedicated-account
   money goes through a batch.

So the report has two halves that must each balance on their own: what was
*taken* today, and what was *received* today. They are not the same money and
the report never pretends they are.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from recon.enums import Channel
from recon.fees import settlement_date
from recon.match.ledger import TxnRow
from recon.match.result import Match
from recon.money import Money, sum_money

#: Money that goes through Paystack and therefore through a settlement batch.
SETTLES_THROUGH_PAYSTACK: frozenset[Channel] = frozenset({Channel.CARD, Channel.DVA})


@dataclass(frozen=True, slots=True)
class ChannelLine:
    channel: Channel
    count: int
    gross: Money
    fees: Money

    @property
    def net(self) -> Money:
        return self.gross - self.fees

    @property
    def settles_later(self) -> bool:
        return self.channel in SETTLES_THROUGH_PAYSTACK


@dataclass(frozen=True, slots=True)
class SettlementLine:
    """One batch that landed in the bank, and the day it was earned."""

    batch_id: str
    earned_on: date | None
    landed_on: date
    gross: Money
    fees: Money
    net: Money
    transaction_count: int

    @property
    def days_late(self) -> int | None:
        if self.earned_on is None:
            return None
        return (self.landed_on - self.earned_on).days


@dataclass
class DailyReport:
    day: date

    taken: list[ChannelLine] = field(default_factory=list)
    received: list[SettlementLine] = field(default_factory=list)

    refunds: Money = field(default_factory=Money.zero)
    reversals: Money = field(default_factory=Money.zero)

    matched_count: int = 0
    matched_money: Money = field(default_factory=Money.zero)
    queued_count: int = 0
    queued_money: Money = field(default_factory=Money.zero)

    still_in_the_air: Money = field(default_factory=Money.zero)
    """Taken today, settling on a later working day. This is the timing gap."""

    problems: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- totals

    @property
    def gross_taken(self) -> Money:
        return sum_money([line.gross for line in self.taken])

    @property
    def fees_charged(self) -> Money:
        return sum_money([line.fees for line in self.taken])

    @property
    def net_earned(self) -> Money:
        """What today's trading is actually worth, once Paystack has had its cut."""
        return self.gross_taken - self.fees_charged

    @property
    def cash_in_hand(self) -> Money:
        """Money already in the till or the main account. Never settles."""
        return sum_money([line.net for line in self.taken if not line.settles_later])

    @property
    def received_today(self) -> Money:
        return sum_money([line.net for line in self.received])

    @property
    def balances(self) -> bool:
        """Does the day add up.

        Everything taken is either in hand already or on its way. If that is not
        true, a transaction has a channel we do not understand, and the report
        says so instead of hiding it.
        """
        return self.cash_in_hand + self.still_in_the_air == self.net_earned


def build(
    day: date,
    transactions: list[TxnRow],
    matches: list[Match],
    settlements: list[dict[str, Any]] | None = None,
) -> DailyReport:
    """The report for one day, from the day's payments and their matches."""
    report = DailyReport(day=day)
    todays = [txn for txn in transactions if txn.paid_at.date() == day]
    by_reference = {txn.reference: txn for txn in transactions}

    counts: dict[Channel, int] = defaultdict(int)
    gross: dict[Channel, Money] = defaultdict(Money.zero)
    fees: dict[Channel, Money] = defaultdict(Money.zero)

    for txn in todays:
        if txn.status in ("failed", "reversed"):
            if txn.status == "reversed":
                report.reversals = report.reversals + txn.amount
            continue

        channel = Channel(txn.channel)
        counts[channel] += 1
        gross[channel] = gross[channel] + txn.amount
        fees[channel] = fees[channel] + txn.fees

        if channel in SETTLES_THROUGH_PAYSTACK:
            lands = settlement_date(day, channel)
            if lands > day:
                report.still_in_the_air = report.still_in_the_air + (txn.amount - txn.fees)

    report.taken = [
        ChannelLine(channel, counts[channel], gross[channel], fees[channel])
        for channel in sorted(counts, key=str)
    ]

    for match in matches:
        matched = by_reference.get(match.transaction_reference)
        if matched is None or matched.paid_at.date() != day:
            continue
        if match.resolved:
            report.matched_count += 1
            report.matched_money = report.matched_money + matched.amount
        else:
            report.queued_count += 1
            report.queued_money = report.queued_money + matched.amount

    report.received = _batches_landing(day, settlements or [], transactions)
    report.problems = _check(report, todays, settlements or [], transactions)
    return report


def _batches_landing(
    day: date, settlements: list[dict[str, Any]], transactions: list[TxnRow]
) -> list[SettlementLine]:
    earned_on: dict[str, set[date]] = defaultdict(set)
    for txn in transactions:
        if txn.settlement_id:
            earned_on[txn.settlement_id].add(txn.paid_at.date())

    lines: list[SettlementLine] = []
    for batch in settlements:
        landed = date.fromisoformat(str(batch["settled_at"])[:10])
        if landed != day:
            continue
        days = sorted(earned_on.get(str(batch["id"]), ()))
        lines.append(
            SettlementLine(
                batch_id=str(batch["id"]),
                earned_on=days[0] if days else None,
                landed_on=landed,
                gross=Money(int(batch["gross_kobo"])),
                fees=Money(int(batch["fees_kobo"])),
                net=Money(int(batch["net_kobo"])),
                transaction_count=int(batch.get("transaction_count", 0)),
            )
        )
    return sorted(lines, key=lambda line: line.batch_id)


def _check(
    report: DailyReport,
    todays: list[TxnRow],
    settlements: list[dict[str, Any]],
    transactions: list[TxnRow],
) -> list[dict[str, Any]]:
    """Everything that does not add up. An empty list is the whole point."""
    problems: list[dict[str, Any]] = []

    if not report.balances:
        problems.append(
            {
                "kind": "day_does_not_balance",
                "detail": (
                    f"net earned {report.net_earned} but "
                    f"{report.cash_in_hand} in hand + {report.still_in_the_air} in the air"
                ),
            }
        )

    members: dict[str, list[TxnRow]] = defaultdict(list)
    for txn in transactions:
        if txn.settlement_id:
            members[txn.settlement_id].append(txn)

    for line in report.received:
        rows = members.get(line.batch_id, [])
        gross = sum_money([txn.amount for txn in rows])
        fees = sum_money([txn.fees for txn in rows])
        if gross != line.gross or fees != line.fees:
            problems.append(
                {
                    "kind": "settlement_does_not_match_its_transactions",
                    "batch": line.batch_id,
                    "detail": (
                        f"batch says {line.gross} gross / {line.fees} fees; "
                        f"its {len(rows)} transactions add up to {gross} / {fees}"
                    ),
                }
            )
        # Net is gross minus fees minus anything refunded out of the batch.
        refunded = sum_money([txn.refunded for txn in rows])
        expected_net = gross - fees - refunded
        if line.net != expected_net:
            problems.append(
                {
                    "kind": "settlement_net_is_not_gross_minus_fees",
                    "batch": line.batch_id,
                    "detail": (
                        f"batch net {line.net}, but its transactions come to "
                        f"{gross} gross - {fees} fees - {refunded} refunded "
                        f"= {expected_net}"
                    ),
                }
            )

    unverified = [
        txn for txn in todays if not txn.verified and Channel(txn.channel) is not Channel.CASH
    ]
    if unverified:
        problems.append(
            {
                "kind": "unverified_payments",
                "count": len(unverified),
                "money": str(sum_money([txn.amount for txn in unverified])),
                "detail": "these were recorded from a webhook that no verify call confirmed",
            }
        )

    return problems


def summarise(report: DailyReport) -> dict[str, Any]:
    """The report as plain data, for a page, a JSON endpoint, or a print-out."""
    return {
        "day": report.day.isoformat(),
        "taken_today": {
            "gross": str(report.gross_taken),
            "paystack_fees": str(report.fees_charged),
            "net": str(report.net_earned),
            "by_channel": [
                {
                    "channel": str(line.channel),
                    "payments": line.count,
                    "gross": str(line.gross),
                    "fees": str(line.fees),
                    "net": str(line.net),
                    "when_it_arrives": "already here"
                    if not line.settles_later
                    else "next working day",
                }
                for line in report.taken
            ],
        },
        "received_today": {
            "total": str(report.received_today),
            "batches": [
                {
                    "batch": line.batch_id,
                    "earned_on": line.earned_on.isoformat() if line.earned_on else None,
                    "days_late": line.days_late,
                    "gross": str(line.gross),
                    "fees": str(line.fees),
                    "net": str(line.net),
                    "payments": line.transaction_count,
                }
                for line in report.received
            ],
        },
        "why_they_differ": {
            "already_in_hand": str(report.cash_in_hand),
            "still_with_paystack": str(report.still_in_the_air),
            "refunded_today": str(report.refunds),
            "reversed_today": str(report.reversals),
        },
        "matching": {
            "closed": report.matched_count,
            "closed_money": str(report.matched_money),
            "needs_a_person": report.queued_count,
            "money_at_risk": str(report.queued_money),
        },
        "balances": report.balances,
        "problems": report.problems,
    }
