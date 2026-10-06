"""Logging in, signing up, and the two pages only an admin sees.

**People** is who has access: new sign-ups waiting to be let in, everyone who
is in, and everyone who was let go. **Activity** is everything anybody did,
newest first, read straight from the append-only audit log, so it cannot say
anything the log does not.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from recon import accounts
from recon.accounts import AccountError
from recon.config import Settings, get_settings
from recon.db import session_scope
from recon.models import AuditRecord, User
from recon.review.access import COOKIE, Viewer, admin_only, current_viewer
from recon.review.web import templates

router = APIRouter()

#: Rows on one page of the activity log.
ACTIVITY_PAGE = 200


# ------------------------------------------------------------ logging in


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/review") -> Any:
    return templates.TemplateResponse(request, "login.html", {"next": _safe(next)})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/review"),
) -> Any:
    try:
        with session_scope() as session:
            _, token = accounts.log_in(session, email=email, password=password)
    except AccountError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe(next), "email": email, "problem": str(exc)},
            status_code=401,
        )
    response = RedirectResponse(_safe(next), status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(accounts.SESSION_LENGTH.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    with session_scope() as session:
        accounts.log_out(session, request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "signup.html", {})


@router.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    password: str = Form(default=""),
) -> Any:
    try:
        with session_scope() as session:
            accounts.sign_up(session, name=name, email=email, password=password)
    except AccountError as exc:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"name": name, "email": email, "problem": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(request, "signup.html", {"done": True})


# --------------------------------------------------------------- your account


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, viewer: Viewer = Depends(current_viewer)) -> Any:
    return templates.TemplateResponse(request, "account.html", {"me": viewer})


@router.post("/account/name", response_class=HTMLResponse)
def change_name(
    request: Request, name: str = Form(default=""), viewer: Viewer = Depends(current_viewer)
) -> Any:
    if viewer.id is None:
        return RedirectResponse("/account", status_code=303)
    try:
        with session_scope() as session:
            user = session.get(User, viewer.id)
            assert user is not None
            accounts.rename(session, user, name)
    except AccountError as exc:
        return templates.TemplateResponse(
            request, "account.html", {"me": viewer, "problem": str(exc)}, status_code=400
        )
    return RedirectResponse("/account", status_code=303)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current: str = Form(default=""),
    new: str = Form(default=""),
    viewer: Viewer = Depends(current_viewer),
) -> Any:
    if viewer.id is None:
        return templates.TemplateResponse(
            request,
            "account.html",
            {"me": viewer, "problem": "The demo has no password to change."},
            status_code=400,
        )
    try:
        with session_scope() as session:
            user = session.get(User, viewer.id)
            assert user is not None
            accounts.change_password(session, user, current=current, new=new)
    except AccountError as exc:
        return templates.TemplateResponse(
            request, "account.html", {"me": viewer, "problem": str(exc)}, status_code=400
        )
    return RedirectResponse("/login?changed=1", status_code=303)


# --------------------------------------------------------------------- admin


@router.get("/admin/people", response_class=HTMLResponse)
def people_page(
    request: Request,
    admin: Viewer = Depends(admin_only),
    settings: Settings = Depends(get_settings),
) -> Any:
    return _people(request, settings)


@router.post("/admin/people/{user_id}/{action}", response_class=HTMLResponse)
def change_access(
    request: Request,
    user_id: int,
    action: str,
    admin: Viewer = Depends(admin_only),
    settings: Settings = Depends(get_settings),
) -> Any:
    try:
        with session_scope() as session:
            me = session.get(User, admin.id)
            assert me is not None
            accounts.change(session, me, user_id, action)
    except AccountError as exc:
        return _people(request, settings, problem=str(exc), status=400)
    return RedirectResponse("/admin/people", status_code=303)


@router.get("/admin/activity", response_class=HTMLResponse)
def activity_page(
    request: Request, who: str = "", page: int = 1, admin: Viewer = Depends(admin_only)
) -> Any:
    page = max(page, 1)
    with session_scope() as session:
        names = {user.email: user.name for user in session.query(User).all()}
        query = session.query(AuditRecord)
        if who:
            query = query.filter(AuditRecord.actor == who)
        total = query.count()
        rows = (
            query.order_by(AuditRecord.at.desc(), AuditRecord.id.desc())
            .offset((page - 1) * ACTIVITY_PAGE)
            .limit(ACTIVITY_PAGE)
            .all()
        )
        entries = [
            {
                "at": row.at.strftime("%d %b %Y, %H:%M"),
                "who": names.get(row.actor, row.actor),
                "actor": row.actor,
                "what": row.action.replace("_", " "),
                "subject": row.subject_id,
                "detail": _detail(row),
            }
            for row in rows
        ]
        actors = sorted({actor for (actor,) in session.query(AuditRecord.actor).distinct().all()})
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "entries": entries,
            "who": who,
            "actors": [(actor, names.get(actor, actor)) for actor in actors],
            "page": page,
            "pages": max(1, -(-total // ACTIVITY_PAGE)),
            "total": total,
        },
    )


# ------------------------------------------------------------------ helpers


def _people(
    request: Request, settings: Settings, problem: str | None = None, status: int = 200
) -> Any:
    with session_scope() as session:
        people = [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": str(user.role),
                "status": str(user.status),
                "joined": user.created_at.strftime("%d %b %Y"),
                "last_login": user.last_login_at.strftime("%d %b %Y, %H:%M")
                if user.last_login_at
                else "never",
                "decisions": session.query(AuditRecord)
                .filter(
                    AuditRecord.actor == user.email,
                    AuditRecord.action.in_(["approved", "rejected"]),
                )
                .count(),
                "is_the_deployment_admin": user.email == settings.admin_email.strip().lower(),
            }
            for user in accounts.everyone(session)
        ]
    return templates.TemplateResponse(
        request, "people.html", {"people": people, "problem": problem}, status_code=status
    )


def _detail(row: AuditRecord) -> str:
    """The evidence column, short enough to read in a table."""
    try:
        evidence = json.loads(row.evidence_json)
    except ValueError:
        return ""
    if not isinstance(evidence, dict):
        return ""
    parts = [f"{key}: {value}" for key, value in evidence.items() if value not in (None, "", [])]
    text = ", ".join(parts)
    return text if len(text) <= 140 else text[:137] + "..."


def _safe(target: str) -> str:
    """Only send people back to a page on this site."""
    return target if target.startswith("/") and not target.startswith("//") else "/review"
