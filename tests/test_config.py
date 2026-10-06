from __future__ import annotations

import pytest
from pydantic import ValidationError

from recon.config import Settings


def test_a_test_key_is_accepted() -> None:
    settings = Settings(PAYSTACK_SECRET_KEY="sk_test_abc123")
    assert settings.paystack_secret_key == "sk_test_abc123"


def test_a_live_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="test mode only"):
        Settings(PAYSTACK_SECRET_KEY="sk_live_abc123")


@pytest.mark.parametrize("junk", ["", "abc", "pk_test_abc", "sk_abc"])
def test_something_that_is_not_a_secret_key_is_refused(junk: str) -> None:
    with pytest.raises(ValidationError, match="does not look like"):
        Settings(PAYSTACK_SECRET_KEY=junk)


def test_settings_are_frozen() -> None:
    settings = Settings(PAYSTACK_SECRET_KEY="sk_test_abc123")
    with pytest.raises(ValidationError):
        settings.offline = True


class TestWhereTheBooksAreKept:
    @pytest.mark.parametrize(
        "given",
        ["postgres://u:p@db.internal:5432/railway", "postgresql://u:p@db.internal:5432/railway"],
    )
    def test_a_hosts_postgres_address_gets_the_driver_named(self, given: str) -> None:
        settings = Settings(PAYSTACK_SECRET_KEY="sk_test_x", RECON_DATABASE_URL=given)
        assert settings.database_url == "postgresql+psycopg://u:p@db.internal:5432/railway"

    def test_the_name_hosts_use_is_read_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RECON_DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgres://u@db/railway")
        assert Settings(PAYSTACK_SECRET_KEY="sk_test_x").database_url.startswith(
            "postgresql+psycopg://"
        )

    def test_our_own_name_wins_when_both_are_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RECON_DATABASE_URL", "sqlite+pysqlite:///./mine.db")
        monkeypatch.setenv("DATABASE_URL", "postgres://u@db/railway")
        assert Settings(PAYSTACK_SECRET_KEY="sk_test_x").database_url.startswith("sqlite")

    def test_the_password_never_shows_up_in_a_log_line(self) -> None:
        settings = Settings(PAYSTACK_SECRET_KEY="sk_test_x", RECON_PASSWORD="hunter2")
        assert "hunter2" not in repr(settings)
