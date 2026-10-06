"""People, passwords and logins.

One business, several people working its books. Anyone can sign up; nobody gets
in until an admin says so. The admin is whoever runs the deployment: the account
named by `RECON_ADMIN_EMAIL` is made an active admin every time the app starts,
so the one person who can fix things can never be locked out of them.

Passwords are hashed with scrypt from the standard library. Only a hash of each
login cookie is stored. Every page load re-reads the account, so removing
someone takes effect on their next click, not when their cookie expires.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from recon import audit
from recon.config import Settings
from recon.enums import AccountStatus, Role
from recon.models import LoginSession, User, utcnow

#: How long a login lasts before it has to be typed again.
SESSION_LENGTH = timedelta(days=14)
MIN_PASSWORD = 10

#: Failed logins allowed per email inside the window, before it waits.
MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 15 * 60

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(ValueError):
    """Something a person typed that cannot be accepted. Safe to show them."""


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
    )
    return hmac.compare_digest(digest.hex(), expected)


#: Checked against when the email is unknown, so a wrong email takes as long to
#: refuse as a wrong password and the timing does not say which accounts exist.
_DECOY_HASH = hash_password(secrets.token_hex(16))


def _clean_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise AccountError("That does not look like an email address.")
    if len(cleaned) > 64:
        raise AccountError("Please use an email address of 64 characters or fewer.")
    return cleaned


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")


# ------------------------------------------------------------------ signing up


def sign_up(session: Session, *, name: str, email: str, password: str) -> User:
    """A new account, waiting for an admin."""
    clean_name = " ".join(name.split())[:100]
    if not clean_name:
        raise AccountError("Please give your name. It goes on every decision you make.")
    address = _clean_email(email)
    _check_password(password)
    if session.query(User).filter(User.email == address).one_or_none() is not None:
        raise AccountError("There is already an account with that email. Try logging in.")

    user = User(
        email=address,
        name=clean_name,
        password_hash=hash_password(password),
        role=Role.STAFF,
        status=AccountStatus.PENDING,
        created_at=utcnow(),
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        action="signed_up",
        subject_type="user",
        subject_id=address,
        actor=address,
        evidence={"name": clean_name},
    )
    return user


def ensure_admin(session: Session, settings: Settings) -> User | None:
    """Make the deployment's admin exist, be active, and be an admin.

    Created with `RECON_PASSWORD` the first time. After that the password is
    left alone, because the admin may have changed it.
    """
    if not settings.admin_email:
        return None
    address = _clean_email(settings.admin_email)
    user = session.query(User).filter(User.email == address).one_or_none()

    if user is None:
        if len(settings.password) < MIN_PASSWORD:
            raise AccountError(
                f"RECON_PASSWORD has to be at least {MIN_PASSWORD} characters to create "
                f"the admin account for {address}."
            )
        user = User(
            email=address,
            name=address.split("@")[0],
            password_hash=hash_password(settings.password),
            role=Role.ADMIN,
            status=AccountStatus.ACTIVE,
            created_at=utcnow(),
            decided_at=utcnow(),
            decided_by="RECON_ADMIN_EMAIL",
        )
        session.add(user)
        session.flush()
        audit.record(
            session,
            action="admin_created",
            subject_type="user",
            subject_id=address,
            evidence={"from": "RECON_ADMIN_EMAIL"},
        )
        return user

    if not (user.is_admin and user.is_active):
        user.role = Role.ADMIN
        user.status = AccountStatus.ACTIVE
        audit.record(
            session,
            action="admin_restored",
            subject_type="user",
            subject_id=address,
            evidence={"from": "RECON_ADMIN_EMAIL"},
        )
    return user


def an_admin_exists(session: Session) -> bool:
    return (
        session.query(User)
        .filter(User.role == Role.ADMIN, User.status == AccountStatus.ACTIVE)
        .count()
        > 0
    )


# ------------------------------------------------------------------ logging in

_failures: dict[str, deque[float]] = defaultdict(deque)


def _too_many_failures(address: str) -> bool:
    recent = _failures[address]
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    while recent and recent[0] < cutoff:
        recent.popleft()
    return len(recent) >= MAX_FAILURES


def forget_failures() -> None:
    """Tests call this between runs."""
    _failures.clear()


def log_in(session: Session, *, email: str, password: str) -> tuple[User, str]:
    """Check the password and start a session. Returns the cookie value.

    The refusal is worded the same whether the email is unknown or the password
    is wrong, so the login box does not tell a stranger who has an account.
    """
    address = email.strip().lower()
    if _too_many_failures(address):
        raise AccountError("Too many wrong tries. Wait fifteen minutes and try again.")

    user = session.query(User).filter(User.email == address).one_or_none()
    if user is None:
        password_matches(password, _DECOY_HASH)
        _failures[address].append(time.monotonic())
        raise AccountError("That email and password do not match an account.")
    if not password_matches(password, user.password_hash):
        _failures[address].append(time.monotonic())
        raise AccountError("That email and password do not match an account.")

    if user.status is AccountStatus.PENDING:
        raise AccountError("Your account is waiting for an admin to approve it.")
    if user.status is AccountStatus.DISABLED:
        raise AccountError("This account no longer has access. Ask an admin.")

    _failures.pop(address, None)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        LoginSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + SESSION_LENGTH,
        )
    )
    user.last_login_at = now
    return user, token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_for(session: Session, token: str | None) -> User | None:
    """The active person behind a cookie, or nobody."""
    if not token:
        return None
    login = session.get(LoginSession, _token_hash(token))
    if login is None or login.expires_at <= utcnow():
        return None
    user = session.get(User, login.user_id)
    if user is None or not user.is_active:
        return None
    return user


def log_out(session: Session, token: str | None) -> None:
    if token:
        login = session.get(LoginSession, _token_hash(token))
        if login is not None:
            session.delete(login)
            # Written now, so nothing later in this request still finds it.
            session.flush()


def rename(session: Session, user: User, name: str) -> None:
    """What other people see. Decisions stay signed with the email."""
    clean = " ".join(name.split())[:100]
    if not clean:
        raise AccountError("A name cannot be blank.")
    audit.record(
        session,
        action="renamed",
        subject_type="user",
        subject_id=user.email,
        actor=user.email,
        evidence={"from": user.name, "to": clean},
    )
    user.name = clean


def change_password(session: Session, user: User, *, current: str, new: str) -> None:
    if not password_matches(current, user.password_hash):
        raise AccountError("Your current password is not right.")
    _check_password(new)
    user.password_hash = hash_password(new)
    # Every other browser logged in as this person has to log in again.
    session.query(LoginSession).filter(LoginSession.user_id == user.id).delete()
    audit.record(
        session,
        action="password_changed",
        subject_type="user",
        subject_id=user.email,
        actor=user.email,
    )


# ------------------------------------------------------------------ admin acts


@dataclass(frozen=True, slots=True)
class Change:
    status: AccountStatus | None = None
    role: Role | None = None


CHANGES: dict[str, Change] = {
    "approve": Change(status=AccountStatus.ACTIVE),
    "remove": Change(status=AccountStatus.DISABLED),
    "restore": Change(status=AccountStatus.ACTIVE),
    "make_admin": Change(role=Role.ADMIN),
    "make_staff": Change(role=Role.STAFF),
}


def change(session: Session, admin: User, user_id: int, action: str) -> User:
    """An admin changes someone's access. Refuses to leave nobody in charge."""
    if action not in CHANGES:
        raise AccountError(f"Unknown action {action!r}.")
    user = session.get(User, user_id)
    if user is None:
        raise AccountError("No such person.")

    wanted = CHANGES[action]
    loses_admin = (
        user.is_admin
        and user.is_active
        and (wanted.role is Role.STAFF or wanted.status is AccountStatus.DISABLED)
    )
    if loses_admin and _active_admins(session) <= 1:
        raise AccountError(
            "That would leave nobody who can let people in. Make someone else admin first."
        )

    if wanted.status is not None:
        user.status = wanted.status
        user.decided_at = utcnow()
        user.decided_by = admin.email
    if wanted.role is not None:
        user.role = wanted.role
    if user.status is not AccountStatus.ACTIVE:
        session.query(LoginSession).filter(LoginSession.user_id == user.id).delete()

    audit.record(
        session,
        action=f"user_{action}",
        subject_type="user",
        subject_id=user.email,
        actor=admin.email,
        evidence={"status": str(user.status), "role": str(user.role)},
    )
    return user


def _active_admins(session: Session) -> int:
    return (
        session.query(User)
        .filter(User.role == Role.ADMIN, User.status == AccountStatus.ACTIVE)
        .count()
    )


def everyone(session: Session) -> list[User]:
    order = {AccountStatus.PENDING: 0, AccountStatus.ACTIVE: 1, AccountStatus.DISABLED: 2}
    return sorted(
        session.query(User).all(),
        key=lambda u: (order[u.status], u.role is not Role.ADMIN, u.name.lower()),
    )


def waiting(session: Session) -> int:
    return session.query(User).filter(User.status == AccountStatus.PENDING).count()


def expired_before(session: Session, moment: datetime) -> int:
    """Clear out logins that ran out. Returns how many."""
    return session.query(LoginSession).filter(LoginSession.expires_at <= moment).delete()
