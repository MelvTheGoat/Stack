"""Who may open the pages.

On the demo, anyone: the customers are made up. On a business's own books the
pages show real customers' names and real money, so they sit behind a password,
`RECON_PASSWORD`. Until one is set they do not open at all. A page that is
public by accident is worse than a page that asks to be set up first.

One shared password, and any name. The name is whatever the person types into
the login box, and it goes on every decision they make, which is the reason for
asking. It is not identity in the strong sense: anyone with the password can
type a colleague's name. For a shop with three people at the till that is the
right trade. Anything bigger wants real accounts.

The webhook and the health check are never behind this. Paystack has no
password to give, and its signature is the lock on that door.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from recon.config import Settings, get_settings

_basic = HTTPBasic(auto_error=False, realm="Reckon")

#: What a decision is signed with when nobody had to log in.
DEMO_REVIEWER = "demo"


class PagesLockedError(Exception):
    """Own books, and no password set. Shown as a page that says what to do."""


def reviewer(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    settings: Settings = Depends(get_settings),
) -> str:
    """The name of the person at the page, or a refusal."""
    if not settings.password:
        if settings.demo:
            return DEMO_REVIEWER
        raise PagesLockedError

    if credentials is None or not hmac.compare_digest(
        credentials.password.encode(), settings.password.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Wrong password. Any name will do; the password is RECON_PASSWORD.",
            headers={"WWW-Authenticate": 'Basic realm="Reckon"'},
        )
    name = " ".join(credentials.username.split())[:64]
    return name or "reviewer"
