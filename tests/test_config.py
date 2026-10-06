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
