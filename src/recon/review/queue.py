"""The list a person works through after closing.

Ordered by money at risk, biggest first, because a bookkeeper has about two
hours and roughly forty decisions in them. Spending that on forty ₦2,000
payments while a ₦400,000 one waits until tomorrow is the wrong evening.

Each item carries the candidates, the confidence, and the reasons in words. A
reviewer should be able to decide without opening another tab.

Rejections are the valuable output. "Wrong customer" on a case the model liked
is a labelled example of exactly the mistake it makes, and those are far harder
to come by than approvals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from recon import audit
from recon.enums import DecisionStatus, Layer, RejectReason
from recon.match.ledger import TxnRow
from recon.match.result import Match
from recon.models import MatchDecision, utcnow
from recon.money import Money, sum_money


@dataclass(frozen=True, slots=True)
class Candidate:
    order_references: tuple[str, ...]
    customer: str
    invoice_total: str
    confidence: float
    found_because: str
    why: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueueItem:
    transaction_reference: str
    paid: Money
    channel: str
    paid_at: str
    narration: str
    payer_name: str
    money_at_risk: Money
    confidence: float
    reason: str
    candidates: tuple[Candidate, ...] = ()
    duplicate_of: str | None = None

    @property
    def is_suspected_duplicate(self) -> bool:
        return self.duplicate_of is not None

    @property
    def has_a_suggestion(self) -> bool:
        return bool(self.candidates)


@dataclass
class Queue:
    items: list[QueueItem] = field(default_factory=list)

    @property
    def money_at_risk(self) -> Money:
        return sum_money([item.money_at_risk for item in self.items])

    def top(self, count: int) -> list[QueueItem]:
        return self.items[:count]

    def money_in_the_top(self, count: int) -> Money:
        return sum_money([item.money_at_risk for item in self.top(count)])


def build(matches: list[Match], transactions: list[TxnRow]) -> Queue:
    """Everything a layer refused to close, worst first."""
    by_reference = {txn.reference: txn for txn in transactions}
    items: list[QueueItem] = []

    for match in matches:
        if match.resolved:
            continue
        txn = by_reference.get(match.transaction_reference)
        if txn is None:
            continue
        evidence = match.evidence
        items.append(
            QueueItem(
                transaction_reference=txn.reference,
                paid=txn.amount,
                channel=txn.channel,
                paid_at=txn.paid_at.isoformat(timespec="minutes"),
                narration=txn.narration,
                payer_name=txn.payer_name,
                money_at_risk=match.money_at_risk,
                confidence=match.confidence,
                reason=str(evidence.get("reason") or evidence.get("note") or "needs a look"),
                candidates=tuple(_candidates(evidence)),
                duplicate_of=evidence.get("duplicate_of"),
            )
        )

    items.sort(key=lambda item: (-item.money_at_risk.kobo, item.transaction_reference))
    return Queue(items=items)


def _candidates(evidence: dict[str, Any]) -> list[Candidate]:
    raw = evidence.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[Candidate] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(
            Candidate(
                order_references=tuple(entry.get("orders", ())),
                customer=str(entry.get("customer", "")),
                invoice_total=str(entry.get("invoice_total", "")),
                confidence=float(entry.get("confidence", 0.0)),
                found_because=str(entry.get("found_because", "")),
                why=tuple(entry.get("why", ())),
            )
        )
    return out


# ------------------------------------------------------------- decisions


def approve(
    session: Session,
    item: QueueItem,
    order_references: list[str],
    who: str,
    note: str = "",
) -> MatchDecision:
    """A person says yes. Their name goes on it."""
    return _record(
        session,
        item,
        order_references=order_references,
        status=DecisionStatus.APPROVED,
        who=who,
        reason=None,
        note=note,
    )


def reject(
    session: Session,
    item: QueueItem,
    reason: RejectReason,
    who: str,
    note: str = "",
) -> MatchDecision:
    """A person says no, and says why.

    The reason code is the whole point. An approval tells the model it was right
    about something it was already fairly sure of; a rejection tells it exactly
    which kind of mistake it made, on a case it got wrong. There are always far
    fewer of those.
    """
    return _record(
        session,
        item,
        order_references=[],
        status=DecisionStatus.REJECTED,
        who=who,
        reason=reason,
        note=note,
    )


def _record(
    session: Session,
    item: QueueItem,
    *,
    order_references: list[str],
    status: DecisionStatus,
    who: str,
    reason: RejectReason | None,
    note: str,
) -> MatchDecision:
    import json

    existing = (
        session.query(MatchDecision)
        .filter(MatchDecision.transaction_reference == item.transaction_reference)
        .one_or_none()
    )
    payload = {
        "order_references_json": json.dumps(sorted(order_references)),
        "layer": Layer.HUMAN,
        "confidence": 1.0,
        "status": status,
        "money_at_risk_kobo": item.money_at_risk.kobo,
        "evidence_json": json.dumps(
            {
                "reviewed_note": note,
                "model_confidence": item.confidence,
                "model_suggested": [list(c.order_references) for c in item.candidates],
                "reason_shown_to_reviewer": item.reason,
            },
            sort_keys=True,
        ),
        "decided_at": utcnow(),
        "decided_by": who,
        "reject_reason": reason,
    }

    if existing is None:
        decision = MatchDecision(transaction_reference=item.transaction_reference, **payload)
        session.add(decision)
    else:
        # A person changing their mind is a new audit row, not a lost one.
        for key, value in payload.items():
            setattr(existing, key, value)
        decision = existing

    audit.record(
        session,
        action="approved" if status is DecisionStatus.APPROVED else "rejected",
        subject_type="transaction",
        subject_id=item.transaction_reference,
        layer=Layer.HUMAN,
        confidence=1.0,
        actor=who,
        inputs={
            "money_at_risk_kobo": item.money_at_risk.kobo,
            "model_confidence": item.confidence,
        },
        evidence={
            "orders": sorted(order_references),
            "reject_reason": str(reason) if reason else None,
            "note": note,
        },
    )
    return decision


def rejection_labels(session: Session) -> list[dict[str, Any]]:
    """Everything a person said no to, shaped for the next training run."""
    rows = (
        session.query(MatchDecision)
        .filter(MatchDecision.status == DecisionStatus.REJECTED)
        .order_by(MatchDecision.decided_at)
        .all()
    )
    return [
        {
            "transaction_reference": row.transaction_reference,
            "reason": str(row.reject_reason) if row.reject_reason else None,
            "rejected_suggestion": row.evidence.get("model_suggested", []),
            "model_confidence": row.evidence.get("model_confidence"),
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat(),
        }
        for row in rows
    ]
