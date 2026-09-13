"""Proving a webhook really came from Paystack.

Paystack signs the exact bytes of the request body with your secret key, using
HMAC SHA-512, and puts the hex digest in the `x-paystack-signature` header.

Two rules that are easy to get wrong:

1. Sign the *raw bytes*, not a re-serialised copy of the parsed JSON. Parsing
   and re-dumping changes whitespace and key order, and the digest changes with
   it. So we hash `await request.body()` before anything touches it.
2. Compare with `hmac.compare_digest`, not `==`. A plain `==` returns faster
   when the first characters differ, which leaks how much of a guess was right.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "x-paystack-signature"


class SignatureError(ValueError):
    """The request did not carry a signature we trust."""


def compute_signature(secret_key: str, body: bytes) -> str:
    """The digest Paystack would have produced for this exact body."""
    if not isinstance(body, bytes | bytearray):
        raise SignatureError(
            f"sign the raw request bytes, got {type(body).__name__}. "
            "Re-encoding parsed JSON changes the bytes and breaks the digest."
        )
    return hmac.new(secret_key.encode("utf-8"), bytes(body), hashlib.sha512).hexdigest()


def verify_signature(secret_key: str, body: bytes, header_value: str | None) -> bool:
    """True if `header_value` is the right signature for `body`.

    Returns False rather than raising, because a bad signature is an ordinary
    thing that happens on a public endpoint and the caller answers it with a
    401, not a stack trace.
    """
    if not header_value:
        return False
    expected = compute_signature(secret_key, body)
    return hmac.compare_digest(expected, header_value.strip().lower())


def require_signature(secret_key: str, body: bytes, header_value: str | None) -> None:
    """Same check, but loud. For code paths where a forgery is not routine."""
    if not verify_signature(secret_key, body, header_value):
        raise SignatureError("webhook signature did not match the request body")
