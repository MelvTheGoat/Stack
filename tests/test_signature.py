from __future__ import annotations

import hashlib
import hmac

import pytest

from recon.paystack.signature import (
    SignatureError,
    compute_signature,
    require_signature,
    verify_signature,
)

KEY = "sk_test_0123456789abcdef"
BODY = b'{"event":"charge.success","data":{"reference":"ref_1","amount":125050}}'


def paystack_would_send(body: bytes, key: str = KEY) -> str:
    """Independent reimplementation, so the test is not just calling the code back."""
    return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()


def test_a_real_signature_passes() -> None:
    assert verify_signature(KEY, BODY, paystack_would_send(BODY))


def test_it_is_sha512_not_sha256() -> None:
    assert len(compute_signature(KEY, BODY)) == 128


def test_one_changed_byte_in_the_body_fails() -> None:
    tampered = BODY.replace(b"125050", b"125051")
    assert not verify_signature(KEY, tampered, paystack_would_send(BODY))


def test_a_different_key_fails() -> None:
    assert not verify_signature(KEY, BODY, paystack_would_send(BODY, "sk_test_someone_else"))


@pytest.mark.parametrize("header", [None, "", "   ", "deadbeef", "0" * 128])
def test_missing_or_junk_signatures_fail(header: str | None) -> None:
    assert not verify_signature(KEY, BODY, header)


def test_whitespace_and_case_are_tolerated() -> None:
    signature = paystack_would_send(BODY)
    assert verify_signature(KEY, BODY, f"  {signature.upper()}  ")


def test_reserialised_json_does_not_verify() -> None:
    """The reason we hash raw bytes: same data, different bytes, different digest."""
    import json

    reserialised = json.dumps(json.loads(BODY)).encode()
    assert reserialised != BODY
    assert not verify_signature(KEY, reserialised, paystack_would_send(BODY))


def test_signing_a_string_instead_of_bytes_is_loud() -> None:
    with pytest.raises(SignatureError, match="raw request bytes"):
        compute_signature(KEY, BODY.decode())  # type: ignore[arg-type]


def test_require_signature_raises() -> None:
    with pytest.raises(SignatureError, match="did not match"):
        require_signature(KEY, BODY, "nope")
    require_signature(KEY, BODY, paystack_would_send(BODY))


def test_comparison_is_constant_time() -> None:
    """Guard against someone 'simplifying' compare_digest into ==."""
    import inspect

    from recon.paystack import signature

    assert "compare_digest" in inspect.getsource(signature.verify_signature)
