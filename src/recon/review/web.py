"""The pages a person actually uses.

Two of them. A queue, ordered by money at risk, and a daily report. Server-
rendered HTML with no JavaScript, because this gets used on a phone in a shop
with bad signal, and a page that works is worth more than a page that is
pleasant on a good connection.

State lives in `recon.state`, which holds the corpus, the fitted matcher and the
current matches. In a deployment that reads from Postgres the same pages would
read from there instead; the templates do not care.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from recon.db import session_scope
from recon.enums import RejectReason
from recon.match.threshold import reviews_in_an_evening
from recon.models import MatchDecision
from recon.report import settlement
from recon.review import queue as review_queue
from recon.state import workspace

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter()

REASON_LABELS: tuple[tuple[str, str], ...] = (
    (RejectReason.WRONG_CUSTOMER.value, "Wrong customer"),
    (RejectReason.WRONG_AMOUNT.value, "Wrong amount"),
    (RejectReason.DUPLICATE_PAYMENT.value, "Already paid — this is a repeat"),
    (RejectReason.NOT_A_PAYMENT.value, "Not a payment for us"),
    (RejectReason.ALREADY_SETTLED.value, "That invoice is already settled"),
    (RejectReason.OTHER.value, "Something else"),
)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> Any:
    space = workspace()
    pending = _pending(space.queue)
    budget = reviews_in_an_evening()

    with session_scope() as session:
        decided = session.query(MatchDecision).count()

    remainder = review_queue.Queue(items=pending.items[budget:]).money_at_risk
    share = (
        pending.money_in_the_top(budget).kobo / pending.money_at_risk.kobo
        if pending.money_at_risk.kobo
        else 0.0
    )

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "queue": pending,
            "budget": budget,
            "decided": decided,
            "remainder": remainder,
            "top_share": f"{share:.0%}",
            "reasons": REASON_LABELS,
        },
    )


@router.post("/review/{reference}/approve")
def approve(reference: str, orders: str = Form(default="")) -> RedirectResponse:
    space = workspace()
    item = space.item(reference)
    chosen = [part for part in orders.split(",") if part]
    if item is not None:
        with session_scope() as session:
            review_queue.approve(session, item, chosen, who=space.reviewer)
        space.mark_decided(reference)
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{reference}/reject")
def reject(
    reference: str, reason: str = Form(default=RejectReason.OTHER.value)
) -> RedirectResponse:
    space = workspace()
    item = space.item(reference)
    if item is not None:
        with session_scope() as session:
            review_queue.reject(session, item, RejectReason(reason), who=space.reviewer)
        space.mark_decided(reference)
    return RedirectResponse("/review", status_code=303)


@router.get("/report", response_class=HTMLResponse)
def report_page(request: Request, day: str | None = None) -> Any:
    space = workspace()
    on = date.fromisoformat(day) if day else space.last_trading_day()
    built = settlement.build(on, space.transactions, space.matches, space.settlements)
    return templates.TemplateResponse(request, "report.html", {"r": settlement.summarise(built)})


@router.get("/api/report")
def report_json(day: str | None = None) -> dict[str, Any]:
    space = workspace()
    on = date.fromisoformat(day) if day else space.last_trading_day()
    return settlement.summarise(
        settlement.build(on, space.transactions, space.matches, space.settlements)
    )


@router.get("/api/queue")
def queue_json() -> dict[str, Any]:
    pending = _pending(workspace().queue)
    return {
        "waiting": len(pending.items),
        "money_at_risk": str(pending.money_at_risk),
        "items": [
            {
                "transaction": item.transaction_reference,
                "paid": str(item.paid),
                "channel": item.channel,
                "narration": item.narration,
                "reason": item.reason,
                "confidence": round(item.confidence, 4),
                "candidates": [
                    {
                        "orders": list(candidate.order_references),
                        "customer": candidate.customer,
                        "confidence": round(candidate.confidence, 4),
                        "why": list(candidate.why),
                    }
                    for candidate in item.candidates
                ],
            }
            for item in pending.items
        ],
    }


@router.get("/api/labels")
def labels_json() -> dict[str, Any]:
    """Rejections, shaped for the next training run."""
    with session_scope() as session:
        return {"labels": review_queue.rejection_labels(session)}


def _pending(queue: review_queue.Queue) -> review_queue.Queue:
    space = workspace()
    return review_queue.Queue(
        items=[item for item in queue.items if item.transaction_reference not in space.decided]
    )
