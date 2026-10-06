"""Talking to Paystack.

Two calls. `verify_transaction` is the one that matters most: a webhook tells
you something happened; a verify call tells you what actually happened, from
the source. We never write a payment into the books on the strength of the
webhook body alone, because a webhook body is something anyone who learns your
endpoint can POST at you, and the signature check is the only thing standing
between the two. Belt and braces: check the signature, then go and ask.

`list_transactions` is for catching up: everything Paystack already has, for
the days before the webhook was pointed here, or for a day it was down.

The client is deliberately synchronous. The webhook endpoint answers Paystack
immediately and hands the work to a background thread, so nothing here is on
the request's critical path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from recon.config import Settings, get_settings
from recon.enums import TransactionStatus
from recon.money import Money

#: The most pages one catch-up will fetch. At 100 a page that is 5,000
#: transactions, far more than a small business takes in a month, and a limit
#: that is hit is reported, never silently stopped at.
MAX_PAGES = 50
PER_PAGE = 100


class PaystackUnavailableError(RuntimeError):
    """We could not get an answer from Paystack."""


class VerifyUnavailableError(PaystackUnavailableError):
    """We could not reach Paystack. Not the same as "Paystack said no"."""


class KeyRefusedError(PaystackUnavailableError):
    """Paystack answered, and the answer was that the secret key is wrong."""


@dataclass
class Listing:
    """Transactions as Paystack lists them, and whether that was all of them."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True


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
            status=status_of(str(data.get("status") or "")),
            amount=_kobo(data.get("amount")),
            fees=_kobo(data.get("fees")),
            raw=data,
        )

    def list_transactions(
        self, since: date, until: date | None = None, *, max_pages: int = MAX_PAGES
    ) -> Listing:
        """Every transaction Paystack holds for this key from `since` on.

        Read page by page until Paystack says there are no more pages, or until
        `max_pages`, in which case the listing says it is incomplete.
        """
        if self.offline:
            raise PaystackUnavailableError("RECON_OFFLINE is on, so Paystack is not called")

        listing = Listing()
        for page in range(1, max_pages + 1):
            params: dict[str, str | int] = {
                "perPage": PER_PAGE,
                "page": page,
                "from": since.isoformat(),
            }
            if until is not None:
                params["to"] = until.isoformat()
            try:
                response = self._http().get("/transaction", params=params)
            except httpx.HTTPError as exc:
                raise PaystackUnavailableError(f"could not reach Paystack: {exc}") from exc
            if response.status_code == 401:
                raise KeyRefusedError("Paystack did not accept PAYSTACK_SECRET_KEY")
            if response.status_code != 200:
                raise PaystackUnavailableError(f"Paystack returned {response.status_code}")

            body: Any = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
            listing.rows.extend(rows)

            meta = body.get("meta") if isinstance(body, dict) else None
            page_count = meta.get("pageCount") if isinstance(meta, dict) else None
            if not rows or not isinstance(page_count, int) or page >= page_count:
                return listing

        listing.complete = False
        return listing

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def status_of(word: str) -> TransactionStatus:
    """Paystack's status word, in ours. Anything still in flight is pending."""
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
