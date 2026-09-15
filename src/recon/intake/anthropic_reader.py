"""The last layer: asking a model to read a payment report the rules could not.

This is the "generative last" end of the house rule. It runs only on reports the
patterns gave up on, which on the labelled set is about one in eighteen, so it
is a small bill and a small blast radius.

Three things keep it honest.

**The schema is generated from the Pydantic model, not written out by hand.**
Claude is given `ReportReading.model_json_schema()` through
`output_config.format`, which constrains generation to that shape. The answer is
then validated against the same model in Python. So the thing the model is told
to produce and the thing we accept cannot drift apart, and a well-formed answer
still has to be a *valid* one.

**It is allowed to say no.** `readable: false` with a reason is a first-class
answer, and it becomes a review item. A model that has to produce a payer name
will produce one.

**There is no second attempt.** The SDK retries transport failures — a 429 or a
502 — and that is fine, because re-sending an identical request is not
re-asking a question. What never happens is re-prompting for a better answer.
Asking twice about "someone paid 20k today" does not find the payer; it finds a
model willing to guess at one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from recon.enums import Channel
from recon.intake.schema import ParseError

if TYPE_CHECKING:  # pragma: no cover - import cost only paid when the layer is on
    from anthropic import Anthropic

Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: Extraction is a short, well-specified job, so it runs at low effort. The
#: thinking is what costs money here, not the hundred tokens of answer. Raise it
#: if the held-back reports start failing in ways that look like carelessness
#: rather than genuine ambiguity.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT: Effort = "low"

SYSTEM = """You read short payment reports written by Nigerian shop staff and \
turn them into structured records. The reports arrive as WhatsApp messages or \
transcribed voice notes.

Rules you must not break:

- amount_kobo is a whole number of kobo. 45,000 naira is 4500000 kobo. \
"45k" is 45,000 naira. "2.5m" is 2,500,000 naira.
- If the amount is hedged ("about fifty thousand", "roughly 100k", "like 20k"), \
there is no amount. Set readable to false.
- If the report names more than one amount, or more than one invoice, set \
readable to false. Choosing between them is guessing.
- If you cannot tell who paid, set readable to false. "Someone", "a customer", \
and "Ada's brother" are not payers.
- Never invent a name, an amount, a date or an invoice number. Leaving a field \
null is always better than filling it in.
- Invoice numbers are written INV-0042. "invoice 42" means INV-0042.
- Dates are relative to the date given in the message. Return ISO format.
"""


class ReportReading(BaseModel):
    """What the model is allowed to hand back.

    Deliberately not `PaymentReport`. This one can say "I could not read it",
    which is the answer we most want it to be able to give; `PaymentReport` is
    the validated record and cannot represent a failure.
    """

    model_config = ConfigDict(extra="forbid")

    readable: bool = Field(description="False if anything below would be a guess.")
    why_not: str | None = Field(
        default=None, description="If not readable, what was missing or ambiguous."
    )
    amount_kobo: int | None = Field(default=None, description="Whole kobo. Never a fraction.")
    payer_name: str | None = Field(default=None, description="Who paid. No titles.")
    channel: Channel | None = Field(default=None, description="How the money arrived.")
    order_reference: str | None = Field(default=None, description="e.g. INV-0042")
    paid_on: str | None = Field(default=None, description="ISO date, e.g. 2024-05-17")


@dataclass
class AnthropicReader:
    """Reads one report with Claude, or refuses.

    Satisfies the same `Extractor` protocol as the rule-based reader, so
    `Intake` treats them identically and the evaluation harness reports which
    one read each case.
    """

    client: Anthropic | None = None
    model: str = DEFAULT_MODEL
    effort: Effort = DEFAULT_EFFORT
    name: str = "claude"
    calls: list[str] = field(default_factory=list)
    """Every report sent to the model, in order. The bill, itemised."""

    def _anthropic(self) -> Anthropic:
        if self.client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise ParseError(
                    "the model layer is switched on but the anthropic package is not "
                    'installed; pip install ".[llm]"',
                    "",
                ) from exc
            self.client = Anthropic()
        return self.client

    def extract(self, text: str, today: date) -> dict[str, Any]:
        import anthropic

        self.calls.append(text)
        prompt = f"Today is {today.isoformat()}.\n\nReport: {text}"

        try:
            response = self._anthropic().messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": self.effort},  # schema comes from output_format
                messages=[{"role": "user", "content": prompt}],
                output_format=ReportReading,
            )
        except anthropic.APIStatusError as exc:
            # A failure to reach the model is not a failure to read the report.
            # It goes to a person either way, but the reason has to say which,
            # or an outage looks like a hard message.
            raise ParseError(f"could not reach the model ({exc.status_code})", text) from exc
        except anthropic.APIConnectionError as exc:
            raise ParseError(f"could not reach the model ({exc})", text) from exc

        if response.stop_reason == "refusal":
            raise ParseError("the model declined to read this one", text)

        reading = response.parsed_output
        if reading is None:
            raise ParseError("the model returned nothing that fit the schema", text)

        if not reading.readable:
            raise ParseError(
                f"the model could not read it: {reading.why_not or 'no reason given'}", text
            )

        return {
            "amount_kobo": reading.amount_kobo,
            "payer_name": reading.payer_name,
            "channel": reading.channel or Channel.UNKNOWN,
            "order_reference": reading.order_reference,
            "paid_on": reading.paid_on,
            "source_text": text.strip(),
        }


def available() -> bool:
    """Is there a key to use. Checked before switching the layer on, so the
    absence of a key is a quiet skip rather than a stack trace."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True
