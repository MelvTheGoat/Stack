"""Talking to Paystack.

Only one call matters here: `verify_transaction`. A webhook tells you something
happened; a verify call tells you what actually happened, from the source. We
never write a payment into the books on the strength of the webhook body alone,
because a webhook body is something anyone who learns your endpoint can POST at
you, and the signature check is the only thing standing between the two. Belt
and braces: check the signature, then go and ask.

The client is deliberately synchronous. The webhook endpoint answers Paystack
immediately and hands the work to a background thread, so nothing here is on
the request's critical path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from recon.config import Settings, get_settings
from recon.enums import TransactionStatus
from recon.money import Money


class VerifyUnavailableError(RuntimeError):
    """We could not reach Paystack. Not the same as "Paystack said no"."""


@dataclass(frozen=True, slots=True)
class VerifiedTransaction:
    """What Paystack says is true about a reference, right now."""

    reference: str
    status: TransactionStatus
    amount: Money
    fees: Money
    raw: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.status is TransactionStatus.SUCCESS


class PaystackClient:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self._settings = settings or get_settings()
        self._client = client

    @property
    def offline(self) -> bool:
        return self._settings.offline

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._settings.paystack_base_url,
                timeout=self._settings.verify_timeout_seconds,
                headers={"Authorization": f"Bearer {self._settings.paystack_secret_key}"},
            )
        return self._client

    def verify_transaction(self, reference: str) -> VerifiedTransaction | None:
        """Ask Paystack what this reference really is.

        Returns None when Paystack has never heard of the reference, which is
        the interesting case: it means somebody sent us a webhook for a payment
        that does not exist.

        Raises `VerifyUnavailableError` if the network is down, so the caller
        can retry rather than record a payment as unverified forever.
        """
        if self.offline:
            return None

        try:
            response = self._http().get(f"/transaction/verify/{reference}")
        except httpx.HTTPError as exc:
            raise VerifyUnavailableError(f"could not reach Paystack: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise VerifyUnavailableError(f"Paystack returned {response.status_code}")
        if response.status_code != 200:
            return None

        body: Any = response.json()
        if not isinstance(body, dict) or not body.get("status"):
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            return None

        return VerifiedTransaction(
            reference=str(data.get("reference") or reference),
            status=_status(str(data.get("status") or "")),
            amount=_kobo(data.get("amount")),
            fees=_kobo(data.get("fees")),
            raw=data,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _status(word: str) -> TransactionStatus:
    lowered = word.lower()
    if lowered == "success":
        return TransactionStatus.SUCCESS
    if lowered in ("reversed", "reversal"):
        return TransactionStatus.REVERSED
    if lowered in ("failed", "abandoned"):
        return TransactionStatus.FAILED
    return TransactionStatus.PENDING


def _kobo(value: Any) -> Money:
    """Paystack sends kobo as an integer. If it ever is not, say so out loud."""
    if value in (None, ""):
        return Money.zero()
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"expected whole kobo from Paystack, got {type(value).__name__}: {value!r}"
        )
    return Money(value)
