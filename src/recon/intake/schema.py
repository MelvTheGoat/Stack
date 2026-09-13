"""What a payment report has to look like before it is allowed into the system.

People report payments in sentences. "Ada paid 45k for invoice 42 this morning,
transfer." A WhatsApp message, or a voice note somebody transcribed. That is a
perfectly reasonable way for a human to tell you something and a terrible way
for a ledger to receive it.

So everything that comes in as prose has to become one of these before it goes
anywhere near the matcher, and if it cannot, it goes to a person instead. There
is no third path where a half-parsed report gets quietly filled in with guesses.

The money field is the strict bit. An amount only exists here as whole kobo, and
a report that says "about fifty thousand" has no amount at all — "about" is not
a number, and rounding it on the business's behalf is exactly the sort of
helpfulness that loses money.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from recon.enums import Channel
from recon.money import Money


class PaymentReport(BaseModel):
    """One payment, as reported by a person, once it has survived validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_kobo: Annotated[int, Field(gt=0)]
    """Whole kobo. Never a float, never a range, never "about"."""

    payer_name: Annotated[str, Field(min_length=2, max_length=120)]
    channel: Channel = Channel.UNKNOWN
    order_reference: str | None = None
    paid_on: date | None = None
    note: str = ""

    source_text: str = ""
    """The words the person actually used. Kept so a reviewer can check us."""

    @field_validator("payer_name")
    @classmethod
    def a_name_needs_a_letter_in_it(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not any(character.isalpha() for character in cleaned):
            raise ValueError(f"{value!r} is not a name")
        return cleaned

    @field_validator("order_reference")
    @classmethod
    def tidy_the_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper().replace(" ", "-").replace("/", "-")
        return cleaned or None

    @property
    def amount(self) -> Money:
        return Money(self.amount_kobo)

    def describe(self) -> str:
        parts = [f"{self.payer_name} paid {self.amount}"]
        if self.order_reference:
            parts.append(f"for {self.order_reference}")
        if self.channel is not Channel.UNKNOWN:
            parts.append(f"by {self.channel.value.replace('_', ' ')}")
        if self.paid_on:
            parts.append(f"on {self.paid_on.isoformat()}")
        return " ".join(parts)


def json_schema() -> dict[str, Any]:
    """The schema handed to a model when the generative fallback is used.

    Generated from the class rather than written out by hand, so the thing the
    model is told to produce and the thing we validate against cannot drift
    apart.
    """
    return PaymentReport.model_json_schema()


class ParseError(Exception):
    """We could not turn this text into a payment report, and we will not guess.

    Carries the original text and what specifically was missing, because that is
    what a reviewer needs in order to fix it in five seconds.
    """

    def __init__(
        self,
        reason: str,
        source_text: str,
        missing: list[str] | None = None,
        attempts: list[str] | None = None,
    ):
        self.reason = reason
        self.source_text = source_text
        self.missing = missing or []
        self.attempts = attempts or []
        """Why each layer gave up, in the order they tried.

        The headline `reason` is the first layer's, because "no amount in the
        text" is what a reviewer needs to see. But the later layers' reasons are
        what you need when the question is "why did the model not save this
        one", so none of them get thrown away.
        """
        super().__init__(f"{reason}: {source_text[:120]!r}")

    def as_review_item(self) -> dict[str, Any]:
        return {
            "kind": "unparsed_report",
            "reason": self.reason,
            "missing": self.missing,
            "text": self.source_text,
            "attempts": self.attempts,
        }
