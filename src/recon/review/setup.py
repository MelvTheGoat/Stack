"""The page where a business brings its own books in.

Three ways in, one for each place money and invoices actually come from:

1. Invoices, as a spreadsheet.
2. Paystack payments: new ones by webhook, older ones by pulling the list.
3. Everything Paystack never saw: the bank statement, and cash.

It also says plainly where the data is kept, because the default (a SQLite file
inside the container) is wiped on most hosts every time the app redeploys, and
finding that out after a month of decisions would be the worst way to learn it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import func

from recon import books, imports, ingest, state
from recon.config import Settings, get_settings
from recon.db import session_scope
from recon.models import WebhookEvent
from recon.paystack.client import PaystackClient, PaystackUnavailableError
from recon.review.access import reviewer
from recon.review.web import templates

router = APIRouter(dependencies=[Depends(reviewer)])

#: Far bigger than a year of a small shop's invoices, small enough that a wrong
#: file is refused before it is read into memory twice.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: Where the "pull from Paystack" date starts, by default.
DEFAULT_LOOKBACK = timedelta(days=30)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, settings: Settings = Depends(get_settings)) -> Any:
    return _render(request, settings)


@router.get("/setup/invoices.csv", response_class=PlainTextResponse)
def invoice_template() -> PlainTextResponse:
    return _csv(imports.INVOICE_TEMPLATE, "invoices.csv")


@router.get("/setup/payments.csv", response_class=PlainTextResponse)
def payment_template() -> PlainTextResponse:
    return _csv(imports.PAYMENT_TEMPLATE, "payments.csv")


@router.post("/setup/invoices", response_class=HTMLResponse)
async def upload_invoices(
    request: Request,
    file: UploadFile = File(...),
    who: str = Depends(reviewer),
    settings: Settings = Depends(get_settings),
) -> Any:
    return await _upload(request, file, settings, who, imports.import_invoices)


@router.post("/setup/payments", response_class=HTMLResponse)
async def upload_payments(
    request: Request,
    file: UploadFile = File(...),
    who: str = Depends(reviewer),
    settings: Settings = Depends(get_settings),
) -> Any:
    return await _upload(request, file, settings, who, imports.import_payments)


@router.post("/setup/paystack", response_class=HTMLResponse)
def pull_paystack(
    request: Request,
    since: str = Form(default=""),
    who: str = Depends(reviewer),
    settings: Settings = Depends(get_settings),
) -> Any:
    if settings.demo:
        return _render(request, settings, _problem(DEMO_REFUSAL), status=409)
    if not paystack_connected(settings):
        return _render(
            request,
            settings,
            _problem("Set PAYSTACK_SECRET_KEY to your sk_test_ key first, then restart."),
            status=400,
        )
    try:
        start = date.fromisoformat(since) if since else date.today() - DEFAULT_LOOKBACK
    except ValueError:
        return _render(request, settings, _problem(f"{since!r} is not a date."), status=400)

    client = PaystackClient(settings)
    try:
        with session_scope() as session:
            pulled = ingest.pull_from_paystack(session, start, client, who=who)
    except (PaystackUnavailableError, ingest.UnreadableListingError) as exc:
        return _render(request, settings, _problem(f"Nothing was pulled: {exc}"), status=502)
    finally:
        client.close()

    state.changed()
    return _render(request, settings, _done(pulled.summary()))


# ---------------------------------------------------------------- helpers

DEMO_REFUSAL = (
    "This instance is showing practice data, so there is nowhere to put yours. "
    "Remove RECON_DEMO and set RECON_PASSWORD to use your own books."
)


def paystack_connected(settings: Settings) -> bool:
    return settings.paystack_secret_key != "sk_test_unset"


def stored_in_a_file(settings: Settings) -> bool:
    return settings.database_url.startswith("sqlite")


async def _upload(
    request: Request, file: UploadFile, settings: Settings, who: str, importer: Any
) -> Any:
    if settings.demo:
        return _render(request, settings, _problem(DEMO_REFUSAL), status=409)

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _render(
            request,
            settings,
            _problem("That file is over 5 MB. Split it and upload each part."),
            status=413,
        )

    with session_scope() as session:
        result: imports.Imported = importer(session, data, who=who)
    if not result.ok:
        return _render(
            request, settings, _problem(result.summary(), result.problems[:50]), status=400
        )

    state.changed()
    return _render(request, settings, _done(result.summary()))


def _render(
    request: Request,
    settings: Settings,
    message: dict[str, Any] | None = None,
    status: int = 200,
) -> Any:
    space = state.workspace()
    with session_scope() as session:
        counts = books.counts(session)
        last_webhook = (
            session.query(func.max(WebhookEvent.received_at))
            .filter(WebhookEvent.signature_ok.is_(True))
            .scalar()
        )
        refused = session.query(WebhookEvent).filter(WebhookEvent.signature_ok.is_(False)).count()

    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "message": message,
            "counts": counts,
            "waiting": len(space.queue.items),
            "last_webhook": last_webhook.strftime("%d %b %Y, %H:%M UTC") if last_webhook else None,
            "refused_webhooks": refused,
            "webhook_url": str(request.url_for("paystack_webhook")),
            "paystack_connected": paystack_connected(settings),
            "offline": settings.offline,
            "stored_in_a_file": stored_in_a_file(settings),
            "since": (date.today() - DEFAULT_LOOKBACK).isoformat(),
        },
        status_code=status,
    )


def _done(text: str) -> dict[str, Any]:
    return {"ok": True, "text": text, "details": []}


def _problem(text: str, details: list[str] | None = None) -> dict[str, Any]:
    return {"ok": False, "text": text, "details": details or []}


def _csv(body: str, filename: str) -> PlainTextResponse:
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
