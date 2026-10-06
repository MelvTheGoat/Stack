"""The HTTP surface.

The webhook endpoint, a health check, and the pages a person uses (mounted from
`recon.review.web`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from recon import accounts, ingest, state
from recon.config import Settings, get_settings
from recon.db import init_db, session_scope
from recon.models import utcnow
from recon.paystack.signature import SIGNATURE_HEADER
from recon.review.access import AdminOnlyError, LoginRequiredError, PagesLockedError
from recon.review.people import router as people_router
from recon.review.setup import router as setup_router
from recon.review.web import router as review_router

log = logging.getLogger("recon.webhook")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    _make_sure_there_is_an_admin()
    yield


def _make_sure_there_is_an_admin() -> None:
    """Create or restore the deployment's admin from RECON_ADMIN_EMAIL.

    A bad setting is logged, not raised: crashing here would put the host into a
    restart loop, and the locked page already tells whoever opens the site what
    to fix.
    """
    try:
        with session_scope() as session:
            accounts.ensure_admin(session, get_settings())
            accounts.expired_before(session, utcnow())
    except accounts.AccountError as exc:
        log.error("no admin account: %s", exc)


app = FastAPI(title="Reckon", version="0.1.0", lifespan=lifespan)
app.include_router(review_router)
app.include_router(setup_router)
app.include_router(people_router)


@app.exception_handler(PagesLockedError)
def pages_locked(request: Request, exc: PagesLockedError) -> HTMLResponse:
    return HTMLResponse(LOCKED_PAGE, status_code=503)


@app.exception_handler(LoginRequiredError)
def login_required(request: Request, exc: LoginRequiredError) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "log in first"}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


@app.exception_handler(AdminOnlyError)
def admin_only(request: Request, exc: AdminOnlyError) -> HTMLResponse:
    return HTMLResponse(
        '<p style="font:16px system-ui;margin:40px">That page is for admins. '
        '<a href="/review">Back to the queue</a>.</p>',
        status_code=403,
    )


LOCKED_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reckon needs an admin</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px">
<h1 style="font-size:20px">Reckon needs an admin first</h1>
<p>These pages show your customers' names and payments, so they stay shut until
somebody is in charge of who gets in.</p>
<p>Where this app runs (on Railway: the service's Variables tab), set:</p>
<ul>
<li><code>RECON_ADMIN_EMAIL</code> to your email address</li>
<li><code>RECON_PASSWORD</code> to a password of at least 10 characters</li>
</ul>
<p>Let it restart, open this page again, and log in with that email and password.
That account is the admin: it lets other people in after they sign up.</p>
<p>Want to look around first? Set <code>RECON_DEMO=1</code> to show practice data
instead.</p>
</body>
"""


def db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/review", status_code=307)


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "mode": "test", "books": "demo" if settings.demo else "own"}


@app.post("/webhooks/paystack")
async def paystack_webhook(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    x_paystack_signature: str | None = Header(default=None, alias=SIGNATURE_HEADER),
) -> dict[str, Any]:
    """Answer Paystack straight away, then do the work.

    The body is read as raw bytes before anything parses it, because the
    signature is over those exact bytes.

    The receive step gets its own session, committed here and now, rather than a
    request-scoped one. Starlette runs background tasks *before* a yielded
    dependency is torn down, so a request-scoped session would still be
    uncommitted when the worker went looking for the row it was told to process.
    """
    body = await request.body()

    with session_scope() as session:
        result = ingest.receive(session, body, x_paystack_signature, settings)

    if not result.accepted:
        # 401 rather than 400: this is an authentication failure, and Paystack
        # should not retry a body we will never accept.
        response.status_code = 401
        log.warning("rejected webhook: %s", result.reason)
        return {"status": "rejected", "reason": result.reason}

    if result.duplicate:
        # Already have it. 200 stops the retry schedule, which is the point.
        return {"status": "duplicate", "event_key": result.event_key}

    assert result.event_key is not None
    background.add_task(_process_in_background, result.event_key)
    return {"status": "accepted", "event_key": result.event_key}


def _process_in_background(event_key: str) -> None:
    """Runs after the response has gone out, in its own session.

    Errors are logged rather than raised: there is nobody left to return them
    to, and the failure is already written onto the stored event row.
    """
    try:
        with session_scope() as session:
            ingest.process(session, event_key)
    except Exception:
        log.exception("processing webhook %s failed", event_key)
    finally:
        state.changed()
