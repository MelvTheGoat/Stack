"""People, passwords and logins, without the web pages in the way."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from recon import accounts
from recon.accounts import AccountError
from recon.config import Settings
from recon.enums import AccountStatus, Role
from recon.models import AuditRecord, LoginSession, User, utcnow

PASSWORD = "a long enough password"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    accounts.forget_failures()


def admin(session: Session, email: str = "boss@example.com") -> User:
    user = accounts.ensure_admin(
        session,
        Settings(PAYSTACK_SECRET_KEY="sk_test_x", RECON_ADMIN_EMAIL=email, RECON_PASSWORD=PASSWORD),
    )
    assert user is not None
    return user


def active(session: Session, email: str = "ada@example.com") -> User:
    user = accounts.sign_up(session, name="Ada Okonkwo", email=email, password=PASSWORD)
    accounts.change(session, admin(session), user.id, "approve")
    return user


class TestPasswords:
    def test_a_password_is_never_stored_as_typed(self) -> None:
        stored = accounts.hash_password(PASSWORD)
        assert PASSWORD not in stored
        assert stored.startswith("scrypt$")
        assert accounts.password_matches(PASSWORD, stored)
        assert not accounts.password_matches("a long enough passwore", stored)

    def test_the_same_password_hashes_differently_each_time(self) -> None:
        assert accounts.hash_password(PASSWORD) != accounts.hash_password(PASSWORD)

    def test_a_mangled_hash_matches_nothing(self) -> None:
        assert not accounts.password_matches(PASSWORD, "not a hash")


class TestSigningUp:
    def test_a_new_account_waits_for_an_admin(self, session: Session) -> None:
        user = accounts.sign_up(
            session, name="  Ada   Okonkwo ", email=" Ada@Example.com ", password=PASSWORD
        )
        assert (user.name, user.email) == ("Ada Okonkwo", "ada@example.com")
        assert user.status is AccountStatus.PENDING
        assert user.role is Role.STAFF
        with pytest.raises(AccountError, match="waiting for an admin"):
            accounts.log_in(session, email="ada@example.com", password=PASSWORD)

    @pytest.mark.parametrize(
        ("name", "email", "password", "says"),
        [
            ("", "ada@example.com", PASSWORD, "name"),
            ("Ada", "not-an-email", PASSWORD, "email"),
            ("Ada", "ada@example.com", "short", "10 characters"),
        ],
    )
    def test_what_cannot_be_accepted_says_why(
        self, session: Session, name: str, email: str, password: str, says: str
    ) -> None:
        with pytest.raises(AccountError, match=says):
            accounts.sign_up(session, name=name, email=email, password=password)

    def test_one_account_per_email(self, session: Session) -> None:
        accounts.sign_up(session, name="Ada", email="ada@example.com", password=PASSWORD)
        with pytest.raises(AccountError, match="already an account"):
            accounts.sign_up(session, name="Ada", email="ADA@example.com", password=PASSWORD)


class TestTheDeploymentsAdmin:
    def test_it_is_made_active_and_admin(self, session: Session) -> None:
        boss = admin(session)
        assert boss.is_admin and boss.is_active
        user, token = accounts.log_in(session, email="boss@example.com", password=PASSWORD)
        assert accounts.user_for(session, token) is user

    def test_a_short_password_will_not_make_one(self, session: Session) -> None:
        with pytest.raises(AccountError, match="RECON_PASSWORD"):
            accounts.ensure_admin(
                session,
                Settings(
                    PAYSTACK_SECRET_KEY="sk_test_x",
                    RECON_ADMIN_EMAIL="b@example.com",
                    RECON_PASSWORD="short",
                ),
            )

    def test_a_changed_password_survives_a_restart(self, session: Session) -> None:
        boss = admin(session)
        accounts.change_password(session, boss, current=PASSWORD, new="a brand new password")
        admin(session)  # the app starting again
        accounts.log_in(session, email="boss@example.com", password="a brand new password")

    def test_it_is_put_back_in_charge_if_someone_demoted_it(self, session: Session) -> None:
        boss = admin(session)
        boss.status = AccountStatus.DISABLED
        boss.role = Role.STAFF
        admin(session)
        assert boss.is_admin and boss.is_active

    def test_another_account_with_that_email_is_promoted_not_duplicated(
        self, session: Session
    ) -> None:
        accounts.sign_up(session, name="Boss", email="boss@example.com", password=PASSWORD)
        boss = admin(session)
        assert boss.is_admin and boss.is_active
        assert session.query(User).count() == 1


class TestLoggingIn:
    def test_wrong_password_and_unknown_email_read_the_same(self, session: Session) -> None:
        active(session)
        messages = []
        for email, password in (("ada@example.com", "wrong password!"), ("nobody@x.com", PASSWORD)):
            with pytest.raises(AccountError) as caught:
                accounts.log_in(session, email=email, password=password)
            messages.append(str(caught.value))
        assert messages[0] == messages[1]

    def test_ten_wrong_tries_and_even_the_right_password_waits(self, session: Session) -> None:
        active(session)
        for _ in range(accounts.MAX_FAILURES):
            with pytest.raises(AccountError):
                accounts.log_in(session, email="ada@example.com", password="wrong password!")
        with pytest.raises(AccountError, match="fifteen minutes"):
            accounts.log_in(session, email="ada@example.com", password=PASSWORD)

    def test_only_a_hash_of_the_cookie_is_kept(self, session: Session) -> None:
        active(session)
        _, token = accounts.log_in(session, email="ada@example.com", password=PASSWORD)
        stored = session.query(LoginSession).one()
        assert token not in stored.token_hash

    def test_an_expired_login_is_nobody(self, session: Session) -> None:
        active(session)
        _, token = accounts.log_in(session, email="ada@example.com", password=PASSWORD)
        session.query(LoginSession).one().expires_at = utcnow() - timedelta(seconds=1)
        assert accounts.user_for(session, token) is None

    def test_logging_out_ends_it(self, session: Session) -> None:
        active(session)
        _, token = accounts.log_in(session, email="ada@example.com", password=PASSWORD)
        accounts.log_out(session, token)
        assert accounts.user_for(session, token) is None


class TestWhatAnAdminCanDo:
    def test_removing_someone_logs_them_out_at_once(self, session: Session) -> None:
        ada = active(session)
        _, token = accounts.log_in(session, email="ada@example.com", password=PASSWORD)
        accounts.change(session, admin(session), ada.id, "remove")
        assert accounts.user_for(session, token) is None
        with pytest.raises(AccountError, match="no longer has access"):
            accounts.log_in(session, email="ada@example.com", password=PASSWORD)

    def test_the_last_admin_cannot_be_removed_or_demoted(self, session: Session) -> None:
        boss = admin(session)
        for action in ("remove", "make_staff"):
            with pytest.raises(AccountError, match="nobody who can let people in"):
                accounts.change(session, boss, boss.id, action)

    def test_with_a_second_admin_the_first_can_step_down(self, session: Session) -> None:
        boss = admin(session)
        ada = active(session)
        accounts.change(session, boss, ada.id, "make_admin")
        accounts.change(session, ada, boss.id, "make_staff")
        assert not boss.is_admin

    def test_every_change_is_in_the_audit_log_with_who_made_it(self, session: Session) -> None:
        ada = active(session)
        entry = (
            session.query(AuditRecord)
            .filter(AuditRecord.action == "user_approve", AuditRecord.subject_id == ada.email)
            .one()
        )
        assert entry.actor == "boss@example.com"

    def test_a_password_change_logs_out_every_other_browser(self, session: Session) -> None:
        ada = active(session)
        _, token = accounts.log_in(session, email="ada@example.com", password=PASSWORD)
        accounts.change_password(session, ada, current=PASSWORD, new="another long password")
        assert accounts.user_for(session, token) is None

    def test_the_old_password_is_needed_to_change_it(self, session: Session) -> None:
        ada = active(session)
        with pytest.raises(AccountError, match="current password"):
            accounts.change_password(session, ada, current="guess guess guess", new="x" * 12)
