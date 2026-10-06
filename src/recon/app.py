"""The HTTP surface.

The webhook endpoint, a health check, and the pages a person uses (mounted from
`recon.review.web`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from recon import ingest, state
from recon.config import Settings, get_settings
from recon.db import init_db, session_scope
from recon.paystack.signature import SIGNATURE_HEADER
from recon.review.web import router as review_router

log = logging.getLogger("recon.webhook")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Reckon", version="0.1.0", lifespan=lifespan)
app.include_router(review_router)


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
