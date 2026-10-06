"""Who is looking at the page, and what they may see.

On the practice data anyone may look: the customers are made up. On a
business's own books every page needs a login, and the admin pages need an
admin. Until there is an admin at all (`RECON_ADMIN_EMAIL` and `RECON_PASSWORD`)
the pages stay shut, because nobody could let anyone in.

The webhook and the health check are never behind this. Paystack has no login
to give, and its signature is the lock on that door.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from recon import accounts
from recon.config import Settings, get_settings
from recon.db import session_scope

#: The cookie a login lives in.
COOKIE = "reckon_session"


@dataclass(frozen=True, slots=True)
class Viewer:
    """The person at the page, as the pages need them."""

    name: str
    email: str
    is_admin: bool = False
    id: int | None = None

    @property
    def signs_as(self) -> str:
        """What goes on their decisions and in the audit log."""
        return self.email


#: Who is looking, on the practice data, when nobody has logged in.
DEMO_VIEWER = Viewer(name="Demo", email="demo")


class PagesLockedError(Exception):
    """Own books, and no admin yet. Shown as a page that says what to set."""


class LoginRequiredError(Exception):
    """Nobody logged in. Pages send them to the login box; the API says 401."""


class AdminOnlyError(Exception):
    """Logged in, but this page is for admins."""


def current_viewer(request: Request, settings: Settings = Depends(get_settings)) -> Viewer:
    with session_scope() as session:
        user = accounts.user_for(session, request.cookies.get(COOKIE))
        if user is not None:
            viewer = Viewer(name=user.name, email=user.email, is_admin=user.is_admin, id=user.id)
        elif settings.demo:
            viewer = DEMO_VIEWER
        elif not accounts.an_admin_exists(session):
            raise PagesLockedError
        else:
            raise LoginRequiredError
        if viewer.is_admin:
            request.state.people_waiting = accounts.waiting(session)
    request.state.viewer = viewer
    return viewer


def reviewer(viewer: Viewer = Depends(current_viewer)) -> str:
    """The name a decision, an upload or a pull is signed with."""
    return viewer.signs_as


def admin_only(viewer: Viewer = Depends(current_viewer)) -> Viewer:
    if not viewer.is_admin:
        raise AdminOnlyError
    return viewer
