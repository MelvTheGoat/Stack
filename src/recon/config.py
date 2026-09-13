"""Settings, read from the environment.

The one opinionated bit: this service refuses to start with a live Paystack
key. It is a reconciliation tool, it only ever reads, but a live key in a
process that can be pointed at a database is a bad thing to have lying around.
Test mode is not a limitation here, it is the point.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """The service cannot start with the settings it was given.

    Subclasses ValueError so pydantic folds it into a ValidationError with the
    field name attached, rather than letting it escape as a bare traceback.
    """


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    paystack_secret_key: str = Field(default="sk_test_unset", alias="PAYSTACK_SECRET_KEY")
    paystack_base_url: str = Field(default="https://api.paystack.co", alias="PAYSTACK_BASE_URL")
    database_url: str = Field(default="sqlite+pysqlite:///./recon.db", alias="RECON_DATABASE_URL")
    offline: bool = Field(default=False, alias="RECON_OFFLINE")
    """When true, skip the live verify call. Used by tests and the demo corpus."""

    verify_timeout_seconds: float = Field(default=10.0, alias="RECON_VERIFY_TIMEOUT")

    @field_validator("paystack_secret_key")
    @classmethod
    def must_be_a_test_key(cls, value: str) -> str:
        if value.startswith("sk_live_"):
            raise ConfigError(
                "PAYSTACK_SECRET_KEY is a live key. This service is test mode only. "
                "Use an sk_test_ key."
            )
        if not value.startswith("sk_test_"):
            raise ConfigError(
                f"PAYSTACK_SECRET_KEY does not look like a Paystack secret key "
                f"(got {value[:7]!r}...). It should start with sk_test_."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once. Call `get_settings.cache_clear()` in tests."""
    return Settings()
