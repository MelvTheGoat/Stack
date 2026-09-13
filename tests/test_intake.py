"""Reading payment reports written by people.

The rule under test throughout: when in doubt, hand it to a person. A reader
that invents a payer scores well on accuracy and puts made-up names in the
books.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from recon.enums import Channel
from recon.intake.amounts import AmountError, find_amount
from recon.intake.parse import GenerativeExtractor, Intake, RuleExtractor, default_intake
from recon.intake.schema import ParseError, PaymentReport, json_schema
from recon.money import Money

TODAY = date(2024, 5, 17)  # a Friday
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TestReadingAnAmount:
    @pytest.mark.parametrize(
        ("text", "naira"),
        [
            ("₦45,000", "45000"),
            ("N45000", "45000"),
            ("#45,000", "45000"),
            ("45k", "45000"),
            ("45,000 naira", "45000"),
            ("45000 NGN", "45000"),
            ("2.5k", "2500"),
            ("1.2m", "1200000"),
            ("N1.5m", "1500000"),
            ("₦1,250.50", "1250.50"),
            ("forty five thousand naira", "45000"),
            ("forty-five thousand naira", "45000"),
            ("two hundred thousand naira", "200000"),
            ("ten thousand naira", "10000"),
            ("she paid 45k for INV-0042", "45000"),
        ],
    )
    def test_the_shapes_people_actually_write(self, text: str, naira: str) -> None:
        assert find_amount(text) == Money.from_naira(naira)

    @pytest.mark.parametrize(
        ("text", "because"),
        [
            ("about fifty thousand naira", "hedged"),
            ("around 45k", "hedged"),
            ("roughly ₦100,000", "hedged"),
            ("paid 45k, balance 15k", "more than one amount"),
            ("invoice 42 was settled", "no amount"),
            ("payment received", "no amount"),
            ("", "no amount"),
        ],
    )
    def test_what_it_refuses(self, text: str, because: str) -> None:
        with pytest.raises(AmountError, match=because):
            find_amount(text)

    def test_a_bare_number_is_not_money(self) -> None:
        """Invoice numbers and dates are full of digits."""
        with pytest.raises(AmountError):
            find_amount("invoice 42 on 17/05")

    def test_it_never_produces_a_fraction_of_a_kobo(self) -> None:
        for text in ("₦1,250.50", "2.5k", "1.2m", "0.5k"):
            assert isinstance(find_amount(text).kobo, int)


class TestTheSchema:
    def test_a_valid_report(self) -> None:
        report = PaymentReport(amount_kobo=4_500_000, payer_name="Ada Okonkwo")
        assert report.amount == Money.from_naira("45000")
        assert report.channel is Channel.UNKNOWN

    def test_a_zero_or_negative_amount_is_refused(self) -> None:
        for amount in (0, -1):
            with pytest.raises(ValidationError):
                PaymentReport(amount_kobo=amount, payer_name="Ada Okonkwo")

    def test_a_name_has_to_have_a_letter_in_it(self) -> None:
        with pytest.raises(ValidationError, match="is not a name"):
            PaymentReport(amount_kobo=100, payer_name="1234")

    def test_unexpected_fields_are_refused(self) -> None:
        """A model that helpfully adds a `confidence` field does not get to."""
        with pytest.raises(ValidationError):
            PaymentReport(amount_kobo=100, payer_name="Ada", confidence=0.9)  # type: ignore[call-arg]

    def test_the_reference_is_tidied(self) -> None:
        report = PaymentReport(amount_kobo=100, payer_name="Ada", order_reference=" inv/0042 ")
        assert report.order_reference == "INV-0042"

    def test_the_schema_is_generated_from_the_class(self) -> None:
        """So the thing a model is told to produce cannot drift from the thing
        we validate against."""
        schema = json_schema()
        assert set(schema["required"]) == {"amount_kobo", "payer_name"}
        assert "amount_kobo" in schema["properties"]


class TestReadingAReport:
    def read(self, text: str) -> PaymentReport:
        return default_intake().parse(text, TODAY).report

    def test_an_ordinary_report(self) -> None:
        report = self.read("Ada Okonkwo paid 45k for invoice 42 this morning by transfer")
        assert report.payer_name == "Ada Okonkwo"
        assert report.amount == Money.from_naira("45000")
        assert report.order_reference == "INV-0042"
        assert report.channel is Channel.BANK_TRANSFER
        assert report.paid_on == TODAY

    def test_a_voice_note_with_a_pronoun_in_it(self) -> None:
        report = self.read("so erm Kelechi Udeh he paid me ten thousand naira cash this afternoon")
        assert report.payer_name == "Kelechi Udeh"
        assert report.channel is Channel.CASH

    def test_a_title_is_not_part_of_the_name(self) -> None:
        """ "Mrs Folake Balogun" will never match "Folake Balogun" on the invoice."""
        assert self.read("Alhaji Musa Danjuma sent 200k by transfer").payer_name == "Musa Danjuma"
        assert self.read("Mrs Folake Balogun dropped 30k cash").payer_name == "Folake Balogun"

    @pytest.mark.parametrize(
        ("text", "when"),
        [
            ("Ada paid 45k today", date(2024, 5, 17)),
            ("Ada paid 45k yesterday", date(2024, 5, 16)),
            ("Ada paid 45k last night", date(2024, 5, 16)),
            ("Ada paid 45k day before yesterday", date(2024, 5, 15)),
            ("Ada paid 45k on Monday", date(2024, 5, 13)),
            ("Ada paid 45k on 15/05/2024", date(2024, 5, 15)),
            ("Ada paid 45k on 2024-05-14", date(2024, 5, 14)),
            ("Ada paid 45k", None),
        ],
    )
    def test_dates(self, text: str, when: date | None) -> None:
        assert self.read(text).paid_on == when

    @pytest.mark.parametrize(
        ("text", "channel"),
        [
            ("Ada paid 45k cash", Channel.CASH),
            ("Ada paid 45k by transfer", Channel.BANK_TRANSFER),
            ("Ada paid 45k on the POS", Channel.CARD),
            ("Ada paid 45k into her dedicated account", Channel.DVA),
            ("Ada paid 45k", Channel.UNKNOWN),
        ],
    )
    def test_channels(self, text: str, channel: Channel) -> None:
        assert self.read(text).channel is channel

    def test_the_original_words_are_kept(self) -> None:
        text = "Ada Okonkwo paid 45k today"
        assert self.read(text).source_text == text


class TestWhenItRefuses:
    def refuse(self, text: str) -> ParseError:
        outcome = default_intake().parse_or_review(text, TODAY)
        assert isinstance(outcome, ParseError), f"it read {text!r} and it should not have"
        return outcome

    def test_no_payer_means_no_report(self) -> None:
        assert "payer_name" in self.refuse("someone paid 20k today").missing

    def test_a_hedged_amount_means_no_report(self) -> None:
        assert "hedged" in self.refuse("about fifty thousand naira from Ada Okonkwo").reason

    def test_two_invoices_named_means_no_report(self) -> None:
        """Picking one of two named invoices is guessing."""
        error = self.refuse("Ada Okonkwo paid ₦45,000 for invoice 42 and invoice 43")
        assert "more than one invoice" in error.reason

    def test_a_date_that_is_not_a_date_means_no_report(self) -> None:
        """Dropping the field would file it as "no date given", which is untrue."""
        assert "not a real date" in self.refuse("Ngozi Bello sent 45k on 31/02/2024").reason

    def test_somebody_paying_on_somebody_elses_behalf(self) -> None:
        """A name we can see that is not the payer is worse than no name."""
        self.refuse("payment of 45k received from Ada Okonkwo's brother")
        self.refuse("Chinedu's driver dropped 20k cash")

    def test_an_empty_report(self) -> None:
        assert "empty" in self.refuse("").reason
        assert "empty" in self.refuse("   ").reason

    def test_a_failure_carries_what_a_reviewer_needs(self) -> None:
        item = self.refuse("someone paid 20k today").as_review_item()
        assert item["kind"] == "unparsed_report"
        assert item["text"] == "someone paid 20k today"
        assert item["reason"]


class TestTheGenerativeLayer:
    def intake(self, response: str) -> Intake:
        def complete(prompt: str, schema: dict[str, Any]) -> str:
            assert "amount_kobo" in schema["properties"], "the model gets the real schema"
            return response

        return Intake(rules=RuleExtractor(), generative=GenerativeExtractor(complete=complete))

    def test_it_only_runs_when_the_rules_could_not(self) -> None:
        calls: list[str] = []

        def complete(prompt: str, schema: dict[str, Any]) -> str:
            calls.append(prompt)
            return "{}"

        intake = Intake(rules=RuleExtractor(), generative=GenerativeExtractor(complete=complete))
        intake.parse("Ada Okonkwo paid 45k today", TODAY)
        assert calls == [], "the rules handled it; there was nothing to pay a model for"

    def test_it_reads_what_the_rules_could_not(self) -> None:
        intake = self.intake(
            json.dumps({"amount_kobo": 4_500_000, "payer_name": "Ada Okonkwo", "channel": "cash"})
        )
        parsed = intake.parse("invoice 42: forty-five thousand naira, Ada Okonkwo, cash", TODAY)
        assert parsed.extractor == "generative"
        assert parsed.report.payer_name == "Ada Okonkwo"

    def test_a_model_answer_that_fails_the_schema_is_dropped_not_retried(self) -> None:
        """The whole point. A second attempt at an ambiguous sentence produces a
        more confident guess, not more information."""
        attempts: list[int] = []

        def complete(prompt: str, schema: dict[str, Any]) -> str:
            attempts.append(1)
            return json.dumps({"amount_kobo": -5, "payer_name": "Ada"})

        intake = Intake(rules=RuleExtractor(), generative=GenerativeExtractor(complete=complete))
        with pytest.raises(ParseError):
            intake.parse("something the rules cannot read at all", TODAY)
        assert len(attempts) == 1, "it must not try again"

    def failure(self, response: str) -> ParseError:
        outcome = self.intake(response).parse_or_review("money entered the account", TODAY)
        assert isinstance(outcome, ParseError)
        return outcome

    def test_a_model_that_says_it_cannot_read_it_is_believed(self) -> None:
        error = self.failure(json.dumps({"error": "no clear amount"}))
        assert any("could not read it" in attempt for attempt in error.attempts)

    def test_junk_from_the_model_is_a_refusal_not_a_crash(self) -> None:
        error = self.failure("I think Ada paid about 45k?")
        assert any("did not return JSON" in attempt for attempt in error.attempts)

    def test_a_model_inventing_extra_fields_is_refused(self) -> None:
        error = self.failure(
            json.dumps({"amount_kobo": 100, "payer_name": "Ada", "certainty": "high"})
        )
        assert any("schema rejected" in attempt for attempt in error.attempts)

    def test_the_headline_reason_is_the_human_readable_one(self) -> None:
        """A reviewer wants "no amount in the text", not "the model returned
        JSON that failed validation". Both are kept; only one leads."""
        error = self.failure(json.dumps({"error": "no clear amount"}))
        assert error.reason == "no amount in the text"
        assert len(error.attempts) == 2

    def test_the_default_intake_does_not_call_a_model_at_all(self) -> None:
        """A system that silently starts calling an API is a system with a
        surprise invoice in it."""
        assert default_intake().generative is None


@pytest.fixture(scope="module")
def labelled_report():  # type: ignore[no-untyped-def]
    from recon.evaluation.extraction import evaluate, load_labelled

    return evaluate(load_labelled(FIXTURES / "intake_labelled.json"))


class TestAgainstTheLabelledSet:
    def test_it_never_invents_a_payment(self, labelled_report) -> None:  # type: ignore[no-untyped-def]
        """The one that must not regress. Everything else is inconvenience."""
        assert labelled_report.wrongly_parsed == 0, labelled_report.mistakes

    def test_it_reads_most_of_the_readable_ones(self, labelled_report) -> None:  # type: ignore[no-untyped-def]
        assert labelled_report.exact_match_rate >= 0.9, labelled_report.mistakes

    def test_every_amount_it_reads_is_right(self, labelled_report) -> None:  # type: ignore[no-untyped-def]
        assert labelled_report.amount_accuracy == 1.0

    def test_the_labelled_set_has_a_slice_written_after_the_parser(self) -> None:
        """Tuning a parser against the same examples you score it on is a smoke
        test wearing an evaluation's clothes."""
        rows = json.loads((FIXTURES / "intake_labelled.json").read_text())
        held_back = [row for row in rows if row.get("written_after")]
        assert len(held_back) >= 10
