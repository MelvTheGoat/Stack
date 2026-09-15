"""Turning "Ada paid 45k for invoice 42 this morning" into a typed record.

Same house rule as everywhere else: deterministic first, generative last. Most
payment reports follow a handful of shapes, and patterns handle those for free,
instantly, with no API call and no chance of a made-up answer. Only what the
patterns cannot read is worth sending to a model, and even then the model's
answer is validated against the same schema and dropped if it does not fit.

There is no retry loop. If the first attempt does not produce a valid record,
the text goes to a person with a note saying what was missing. A second attempt
at the same ambiguous sentence does not produce more information, it produces a
more confident guess, and a confident guess about somebody's money is the thing
this whole system exists to avoid.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from recon.enums import Channel
from recon.intake.amounts import AmountError, find_amount
from recon.intake.schema import ParseError, PaymentReport, json_schema

#: This business numbers its invoices INV-0042. Somebody saying "invoice 42"
#: means that one, so the padding lives here rather than in five regexes.
REFERENCE_FORMAT = "INV-{:04d}"

_EXPLICIT_REFERENCE = re.compile(r"\b(INV[-/ ]?\d{1,6})\b", re.IGNORECASE)
_SPOKEN_REFERENCE = re.compile(
    r"\b(?:invoice|inv|order|ref(?:erence)?)\s*(?:number|no\.?|#)?\s*(\d{1,6})\b", re.IGNORECASE
)

_CHANNEL_WORDS: tuple[tuple[Channel, tuple[str, ...]], ...] = (
    (Channel.CASH, ("cash", "in person", "by hand", "physical money", "notes")),
    (Channel.DVA, ("dedicated account", "virtual account", "her account", "his account", "dva")),
    (Channel.CARD, ("card", "pos", "online", "paystack link", "checkout")),
    (Channel.BANK_TRANSFER, ("transfer", "transfered", "transferred", "bank", "nip", "sent it")),
)

# \u2019 is the typographic apostrophe, spelled as an escape so it is obvious
# it is there on purpose: people's names arrive with either kind depending on
# whether the reporter typed them on a phone.

#: Verbs that come straight after the person who paid.
_NAME_BEFORE_VERB = re.compile(
    r"\b(?P<name>(?:[A-Z][\w'\u2019-]+\s+){0,2}[A-Z][\w'\u2019-]+)"
    # Voice notes say "Kelechi Udeh he paid me...". The pronoun is speech, not
    # a different person.
    r",?(?:\s+(?:he|she|they))?\s+"
    r"(?:paid|sent|transferred|transfered|dropped|settled|deposited)\b"
)
#: ... and the shapes where the person comes after a preposition.
_NAME_AFTER_PREPOSITION = re.compile(
    r"\b(?:from|by|for)\s+(?P<name>(?:[A-Z][\w'\u2019-]+\s+){0,2}[A-Z][\w'\u2019-]+)"
)

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_DATE_PATTERNS = (
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"),
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
)

#: Titles people put in front of a name. Kept out of the name itself, because
#: "Mrs Folake Balogun" will not match "Folake Balogun" on the invoice.
_TITLES: frozenset[str] = frozenset(
    {
        "mr",
        "mrs",
        "miss",
        "ms",
        "dr",
        "prof",
        "engr",
        "barr",
        "arc",
        "pastor",
        "rev",
        "imam",
        "alhaji",
        "alhaja",
        "chief",
        "oga",
        "madam",
        "aunty",
        "auntie",
        "uncle",
        "sir",
        "ma",
    }
)

#: "Ada's brother", "Chinedu's rep". The name is in the sentence and it is not
#: the payer, which is worse than no name at all.
_STAND_INS = re.compile(
    r"\b(?:brother|sister|wife|husband|son|daughter|friend|rep|driver|boy|assistant|"
    r"colleague|partner|mother|father|uncle|aunt)\b",
    re.IGNORECASE,
)

#: Words that mean the reporter is not sure who paid. A name we are not sure of
#: is worse than no name, because it looks like evidence.
_VAGUE_NAMES: frozenset[str] = frozenset(
    {"someone", "somebody", "a customer", "the customer", "a man", "a woman", "client"}
)


class Extractor(Protocol):
    """Anything that can turn text into a candidate record, or fail."""

    name: str

    def extract(self, text: str, today: date) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Parsed:
    report: PaymentReport
    extractor: str
    """Which layer read it: "rules" or "generative". Reported separately so the
    generative share can be watched rather than assumed."""


# ------------------------------------------------------------------- rules


@dataclass
class RuleExtractor:
    """Patterns. Handles the ordinary shapes, costs nothing, cannot hallucinate."""

    name: str = "rules"
    known_names: frozenset[str] = frozenset()

    def extract(self, text: str, today: date) -> dict[str, Any]:
        missing: list[str] = []

        try:
            amount = find_amount(text)
        except AmountError as exc:
            raise ParseError(str(exc), text, ["amount"]) from exc

        payer = self._payer(text)
        if payer is None:
            missing.append("payer_name")
        if missing:
            raise ParseError(f"could not find {' and '.join(missing)} in the text", text, missing)

        return {
            "amount_kobo": amount.kobo,
            "payer_name": payer,
            "channel": self._channel(text),
            "order_reference": self._reference(text),
            "paid_on": self._when(text, today),
            "source_text": text.strip(),
        }

    def _payer(self, text: str) -> str | None:
        lowered = text.lower()
        for vague in _VAGUE_NAMES:
            if vague in lowered:
                return None
        if _STAND_INS.search(text) or "\u2019s " in text or "'s " in text:
            # Somebody paid on somebody else's behalf. We know a name and we do
            # not know whose money it is, and those are not the same thing.
            return None

        for known in self.known_names:
            if known.lower() in lowered:
                return known

        for pattern in (_NAME_BEFORE_VERB, _NAME_AFTER_PREPOSITION):
            match = pattern.search(text)
            if match:
                candidate = _strip_titles(match.group("name"))
                if candidate and candidate.lower() not in _VAGUE_NAMES and len(candidate) >= 2:
                    return candidate
        return None

    def _channel(self, text: str) -> Channel:
        lowered = text.lower()
        for channel, words in _CHANNEL_WORDS:
            if any(word in lowered for word in words):
                return channel
        return Channel.UNKNOWN

    def _reference(self, text: str) -> str | None:
        """The one invoice this report is about, or a refusal.

        Same rule as amounts: "invoice 42 and invoice 43" names two, and picking
        one of them is guessing. It goes to a person.
        """
        found = {
            REFERENCE_FORMAT.format(int(re.sub(r"\D", "", match)))
            for match in _EXPLICIT_REFERENCE.findall(text)
            if re.sub(r"\D", "", match)
        }
        found |= {
            REFERENCE_FORMAT.format(int(digits)) for digits in _SPOKEN_REFERENCE.findall(text)
        }

        if len(found) > 1:
            raise ParseError(
                f"the report names more than one invoice ({', '.join(sorted(found))})",
                text,
                ["order_reference"],
            )
        return found.pop() if found else None

    def _when(self, text: str, today: date) -> date | None:
        lowered = text.lower()

        for pattern in _DATE_PATTERNS:
            match = pattern.search(lowered)
            if match:
                parts = [int(p) for p in match.groups()]
                try:
                    if parts[0] > 31:  # yyyy-mm-dd
                        return date(parts[0], parts[1], parts[2])
                    return date(parts[2], parts[1], parts[0])  # dd/mm/yyyy
                except ValueError as exc:
                    # 31/02/2024. The reporter meant a day and got it wrong.
                    # Quietly dropping the field would file the payment under
                    # "no date given", which is a different and untrue thing.
                    raise ParseError(
                        f"the date {match.group(0)!r} is not a real date",
                        text,
                        ["paid_on"],
                    ) from exc

        if "day before yesterday" in lowered:
            return today - timedelta(days=2)
        if "yesterday" in lowered or "last night" in lowered:
            return today - timedelta(days=1)
        if any(
            word in lowered
            for word in ("today", "this morning", "this afternoon", "this evening", "just now")
        ):
            return today

        for index, weekday in enumerate(_WEEKDAYS):
            if weekday in lowered:
                behind = (today.weekday() - index) % 7 or 7
                return today - timedelta(days=behind)
        return None


# -------------------------------------------------------------- generative


Complete = Callable[[str, dict[str, Any]], str]
"""A function that takes a prompt and a JSON schema and returns JSON text.

Deliberately a plain callable. This repo does not pick a model provider for you,
and the parts that matter — the schema, the validation, the refusal to retry —
are the same whichever one you plug in.
"""

PROMPT = """Read this payment report and return JSON matching the schema exactly.

Rules:
- amount_kobo is whole kobo. 45,000 naira is 4500000.
- If the text does not clearly state an amount, or hedges it ("about", "around"),
  return {{"error": "no clear amount"}}.
- If you cannot tell who paid, return {{"error": "no payer"}}.
- Never invent a name, an amount, a date or an invoice number.

Report: {text}
"""


@dataclass
class GenerativeExtractor:
    """Last resort. Schema-constrained, validated, and never retried.

    The model's answer is not trusted because it came back well-formed. It is
    parsed, validated against the same Pydantic model as everything else, and
    thrown away if it does not fit. A model that returns `{"error": ...}` is
    doing the right thing and that becomes a review item, not a second attempt.
    """

    complete: Complete
    name: str = "generative"

    def extract(self, text: str, today: date) -> dict[str, Any]:
        raw = self.complete(PROMPT.format(text=text), json_schema())
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(f"the model did not return JSON: {exc}", text, []) from exc

        if not isinstance(payload, dict):
            raise ParseError("the model returned something that is not an object", text, [])
        if "error" in payload:
            raise ParseError(f"the model could not read it: {payload['error']}", text, [])

        payload.setdefault("source_text", text.strip())
        return payload


# ------------------------------------------------------------------ front door


@dataclass
class Intake:
    """Rules first, model second, a person third."""

    rules: RuleExtractor
    generative: Extractor | None = None
    """Any reader satisfying the protocol. `recon.intake.anthropic_reader` is
    the real one; `GenerativeExtractor` wraps a plain callable for anyone
    plugging in something else."""

    def parse(self, text: str, today: date | None = None) -> Parsed:
        """Read one report. Raises `ParseError` if nothing could read it."""
        when = today or date.today()
        if not text or not text.strip():
            raise ParseError("the report is empty", text, ["amount", "payer_name"])

        failures: list[ParseError] = []
        for extractor in self._layers():
            try:
                fields = extractor.extract(text, when)
                return Parsed(report=PaymentReport(**fields), extractor=extractor.name)
            except ParseError as exc:
                failures.append(exc)
            except ValidationError as exc:
                # The extractor produced something, and it did not fit the
                # schema. That is a failure, not a prompt to try harder.
                failures.append(
                    ParseError(
                        f"{extractor.name} produced something the schema rejected: "
                        f"{_first_problem(exc)}",
                        text,
                        [str(error["loc"][0]) for error in exc.errors() if error.get("loc")],
                    )
                )

        headline = failures[0]
        raise ParseError(
            headline.reason,
            text,
            headline.missing,
            attempts=[
                f"{layer.name}: {failure.reason}"
                for layer, failure in zip(self._layers(), failures, strict=False)
            ],
        )

    def _layers(self) -> list[Extractor]:
        layers: list[Extractor] = [self.rules]
        if self.generative is not None:
            layers.append(self.generative)
        return layers

    def parse_or_review(self, text: str, today: date | None = None) -> Parsed | ParseError:
        """Same thing, without the exception, for batch runs."""
        try:
            return self.parse(text, today)
        except ParseError as exc:
            return exc


def _strip_titles(name: str) -> str:
    words = name.split()
    while words and words[0].lower().rstrip(".") in _TITLES:
        words.pop(0)
    return " ".join(words)


def _first_problem(error: ValidationError) -> str:
    problems = error.errors()
    if not problems:
        return "unknown"
    first = problems[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', '')}".strip(": ")


def default_intake(known_names: frozenset[str] = frozenset()) -> Intake:
    """Rules only. The model layer is opt-in, because it costs money and a
    system that silently starts calling an API is a system with a surprise
    invoice in it."""
    return Intake(rules=RuleExtractor(known_names=known_names))


def intake_with_model(
    known_names: frozenset[str] = frozenset(), model: str | None = None
) -> Intake:
    """Rules first, then Claude on whatever the rules could not read.

    Raises if there is no key, rather than quietly degrading to rules-only and
    reporting a number that looks like the model earned it.
    """
    from recon.intake.anthropic_reader import DEFAULT_MODEL, AnthropicReader, available

    if not available():
        raise RuntimeError(
            "the model layer needs ANTHROPIC_API_KEY set and the anthropic package "
            'installed (pip install ".[llm]")'
        )
    return Intake(
        rules=RuleExtractor(known_names=known_names),
        generative=AnthropicReader(model=model or DEFAULT_MODEL),
    )
