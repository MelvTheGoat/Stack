"""Bringing a business's own invoices and payments in from a spreadsheet."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from recon import imports
from recon.enums import Channel, Layer, OrderStatus
from recon.models import AuditRecord, Customer, Order, Transaction
from recon.money import Money, MoneyError


def csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


class TestReadingAmountsAndDates:
    @pytest.mark.parametrize(
        ("written", "kobo"),
        [
            ("25000", 2_500_000),
            ("25,000.50", 2_500_050),
            ("₦25,000", 2_500_000),
            ("NGN 25000", 2_500_000),
            ("N25000.5", 2_500_050),
        ],
    )
    def test_amounts_as_people_write_them(self, written: str, kobo: int) -> None:
        assert imports.parse_amount(written) == Money(kobo)

    @pytest.mark.parametrize("written", ["", "0", "-500", "12.345", "about 5k"])
    def test_an_amount_that_is_not_a_payment_is_refused(self, written: str) -> None:
        with pytest.raises(MoneyError):
            imports.parse_amount(written)

    @pytest.mark.parametrize(
        "written", ["2024-05-17", "17/05/2024", "17-05-2024", "17 May 2024", "17-May-2024"]
    )
    def test_dates_keep_the_day_as_written(self, written: str) -> None:
        assert imports.parse_when(written) == datetime(2024, 5, 17, tzinfo=UTC)

    def test_a_time_with_a_zone_is_moved_to_utc(self) -> None:
        assert imports.parse_when("2024-05-17T09:00:00+01:00") == datetime(
            2024, 5, 17, 8, 0, tzinfo=UTC
        )

    def test_not_a_date_says_what_would_be(self) -> None:
        with pytest.raises(ValueError, match="2024-05-17"):
            imports.parse_when("yesterday")


class TestInvoices:
    def test_the_template_goes_in_as_it_is(self, session: Session) -> None:
        result = imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode())
        assert result.ok, result.problems
        assert result.added == 2

        ada = session.get(Order, "INV-0001")
        musa = session.get(Order, "INV-0002")
        assert ada is not None and musa is not None
        assert ada.amount == Money.from_naira("25000.00")
        assert musa.amount == Money.from_naira("12500")
        assert ada.issued_at == musa.issued_at == datetime(2024, 5, 17, tzinfo=UTC)
        assert ada.status is OrderStatus.OPEN
        customer = session.get(Customer, ada.customer_id)
        assert customer is not None and customer.name == "Ada Okonkwo"
        assert customer.email == "ada@example.com"

    def test_the_same_file_twice_changes_nothing(self, session: Session) -> None:
        imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode())
        again = imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode())
        assert (again.added, again.updated, again.already_there) == (0, 0, 2)
        assert session.query(Order).count() == 2
        assert session.query(Customer).count() == 2

    def test_a_corrected_amount_updates_the_invoice(self, session: Session) -> None:
        imports.import_invoices(
            session, csv("reference,customer,amount,issued", "INV-1,Ada,500,2024-05-17")
        )
        result = imports.import_invoices(
            session, csv("reference,customer,amount,issued", "INV-1,Ada,550,2024-05-17")
        )
        assert result.updated == 1
        order = session.get(Order, "INV-1")
        assert order is not None and order.amount == Money.from_naira("550")

    def test_one_bad_row_and_nothing_goes_in_and_every_problem_is_listed(
        self, session: Session
    ) -> None:
        result = imports.import_invoices(
            session,
            csv(
                "reference,customer,amount,issued",
                "INV-1,Ada,500,2024-05-17",
                "INV-2,Musa,12.345,2024-05-17",
                "INV-3,,500,2024-05-17",
                "INV-4,Bola,500,yesterday",
            ),
        )
        assert not result.ok
        assert [problem.split(":")[0] for problem in result.problems] == [
            "line 3",
            "line 4",
            "line 5",
        ]
        assert session.query(Order).count() == 0
        assert "Nothing was imported" in result.summary()

    def test_the_same_invoice_twice_in_one_file_is_a_problem(self, session: Session) -> None:
        result = imports.import_invoices(
            session,
            csv(
                "reference,customer,amount,issued",
                "INV-1,Ada,500,2024-05-17",
                "inv-1,Ada,600,2024-05-17",
            ),
        )
        assert result.problems == ["line 3: INV-1 is also on line 2"]

    def test_a_missing_column_is_named(self, session: Session) -> None:
        result = imports.import_invoices(
            session, csv("reference,customer,issued", "INV-1,Ada,2024-05-17")
        )
        assert "amount" in result.problems[0]

    def test_invoice_numbers_are_stored_the_way_the_matcher_spells_them(
        self, session: Session
    ) -> None:
        imports.import_invoices(
            session, csv("invoice,customer,total,date", "inv/0042,Ada,500,2024-05-17")
        )
        assert session.get(Order, "INV-0042") is not None

    def test_an_invoice_already_paid_can_say_so(self, session: Session) -> None:
        imports.import_invoices(
            session, csv("reference,customer,amount,issued,status", "INV-1,Ada,500,2024-05-17,paid")
        )
        order = session.get(Order, "INV-1")
        assert order is not None and order.status is OrderStatus.PAID

    def test_a_file_excel_saved_with_semicolons_still_reads(self, session: Session) -> None:
        result = imports.import_invoices(
            session, csv("reference;customer;amount;issued", "INV-1;Ada;500;17/05/2024")
        )
        assert result.added == 1

    def test_a_file_that_is_not_utf8_says_how_to_save_it(self, session: Session) -> None:
        result = imports.import_invoices(
            session, "reference,customer\nINV-1,Adé\n".encode("cp1252")
        )
        assert "CSV UTF-8" in result.problems[0]

    def test_every_upload_is_written_to_the_audit_log(self, session: Session) -> None:
        imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode(), who="ada")
        entry = session.query(AuditRecord).one()
        assert (entry.action, entry.actor) == ("imported_invoices", "ada")


class TestPayments:
    def test_the_template_goes_in_as_it_is(self, session: Session) -> None:
        result = imports.import_payments(session, imports.PAYMENT_TEMPLATE.encode())
        assert result.ok, result.problems
        assert result.added == 2

        transfer = session.get(Transaction, "FT24139XYZ")
        assert transfer is not None
        assert transfer.channel is Channel.BANK_TRANSFER
        assert transfer.amount == Money.from_naira("25000.00")
        assert transfer.verified, "a statement line is the bank's own record"

        (cash,) = session.query(Transaction).filter(Transaction.channel == Channel.CASH).all()
        assert cash.reference.startswith("cash_")
        assert not cash.verified, "cash is somebody's word"
        assert cash.stated_reference == "INV-0002"

    def test_the_same_file_twice_changes_nothing(self, session: Session) -> None:
        imports.import_payments(session, imports.PAYMENT_TEMPLATE.encode())
        again = imports.import_payments(session, imports.PAYMENT_TEMPLATE.encode())
        assert (again.added, again.already_there) == (0, 2)
        assert session.query(Transaction).count() == 2

    def test_two_identical_lines_are_two_payments(self, session: Session) -> None:
        """A double transfer looks exactly like this, and the duplicate check
        downstream can only catch what was let in."""
        line = "2024-05-18,5000,TRF FROM ADA,ADA OKONKWO"
        result = imports.import_payments(session, csv("date,amount,narration,payer", line, line))
        assert result.added == 2

    def test_a_statements_money_going_out_is_skipped(self, session: Session) -> None:
        result = imports.import_payments(
            session,
            csv(
                "Value Date,Debit,Credit,Remarks",
                "18/05/2024,,25000.00,TRF FROM ADA OKONKWO",
                "18/05/2024,3000.00,,POS PURCHASE",
            ),
        )
        assert (result.added, result.skipped) == (1, 1)
        (txn,) = session.query(Transaction).all()
        assert txn.narration == "TRF FROM ADA OKONKWO"

    def test_card_payments_are_sent_to_paystack_instead(self, session: Session) -> None:
        result = imports.import_payments(
            session, csv("date,amount,channel", "2024-05-18,5000,card")
        )
        assert "Paystack" in result.problems[0]
        assert session.query(Transaction).count() == 0

    def test_a_payment_paystack_already_told_us_about_is_left_alone(self, session: Session) -> None:
        session.add(
            Transaction(
                reference="ref_1",
                channel=Channel.CARD,
                amount_kobo=500_000,
                fees_kobo=8_500,
                paid_at=datetime(2024, 5, 18, tzinfo=UTC),
            )
        )
        session.flush()
        result = imports.import_payments(
            session, csv("date,amount,reference", "2024-05-18,1,ref_1")
        )
        assert result.already_there == 1
        txn = session.get(Transaction, "ref_1")
        assert txn is not None and txn.amount == Money(500_000)


@pytest.mark.usefixtures("own_books")
def test_an_upload_is_matched_on_the_next_page_load() -> None:
    from recon import state
    from recon.db import session_scope

    with session_scope() as session:
        imports.import_invoices(session, imports.INVOICE_TEMPLATE.encode())
        imports.import_payments(session, imports.PAYMENT_TEMPLATE.encode())
    state.changed()

    matches = {m.transaction_reference: m for m in state.workspace().matches}
    transfer = matches["FT24139XYZ"]
    assert transfer.layer is Layer.EXACT_REFERENCE
    assert transfer.order_references == ("INV-0001",)
