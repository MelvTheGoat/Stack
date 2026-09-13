from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from recon.enums import Channel, TransactionStatus
from recon.models import DedicatedAccount, Transaction
from recon.money import Money
from recon.paystack.events import EventError, apply_event, parse_event
from tests import factories


class TestParsing:
    def test_card_charge(self) -> None:
        event = parse_event(factories.charge_success())
        assert event.event_type == "charge.success"
        assert event.reference == "ref_card_1"
        assert event.channel is Channel.CARD
        assert event.amount == Money(125050)
        assert event.fees == Money(1976)
        assert event.status is TransactionStatus.SUCCESS
        assert event.payer_name == "Ada Okonkwo"
        assert event.occurred_at == datetime(2024, 5, 17, 18, 30, tzinfo=UTC)
        assert event.is_handled

    def test_dva_transfer_carries_the_account_number(self) -> None:
        event = parse_event(factories.dva_transfer(account_number="9987654321"))
        assert event.channel is Channel.DVA
        assert event.dva_account_number == "9987654321"
        assert event.payer_name == "ADA OKONKWO"

    def test_main_account_transfer_gives_us_only_a_narration(self) -> None:
        event = parse_event(factories.bank_transfer(narration="TRF/ADA OKONKO/PAYMENT"))
        assert event.channel is Channel.BANK_TRANSFER
        assert event.narration == "TRF/ADA OKONKO/PAYMENT"
        assert event.stated_reference is None  # nothing structured to trust

    def test_a_structured_order_reference_is_picked_up(self) -> None:
        event = parse_event(factories.charge_success(metadata={"order_reference": "INV-0042"}))
        assert event.stated_reference == "INV-0042"

    def test_order_reference_in_a_custom_field(self) -> None:
        event = parse_event(
            factories.charge_success(
                metadata={
                    "custom_fields": [
                        {"display_name": "Phone", "variable_name": "phone", "value": "080"},
                        {
                            "display_name": "Invoice No",
                            "variable_name": "invoice_no",
                            "value": "INV-7",
                        },
                    ]
                }
            )
        )
        assert event.stated_reference == "INV-7"

    def test_metadata_sent_as_a_json_string_still_parses(self) -> None:
        event = parse_event(factories.charge_success(metadata='{"order_reference": "INV-9"}'))  # type: ignore[arg-type]
        assert event.stated_reference == "INV-9"

    def test_a_reference_dug_out_of_a_narration_is_not_trusted_here(self) -> None:
        """Narration scraping is a guess, and guesses belong to the matcher."""
        event = parse_event(factories.bank_transfer(narration="PAYMENT FOR INV-0042"))
        assert event.stated_reference is None
        assert "INV-0042" in event.narration

    def test_body_without_an_event_field_is_loud(self) -> None:
        with pytest.raises(EventError, match="no 'event' field"):
            parse_event({"data": {}})

    def test_a_float_amount_is_loud(self) -> None:
        body = factories.charge_success()
        body["data"]["amount"] = 1250.50
        with pytest.raises(EventError, match="whole kobo"):
            parse_event(body)

    def test_unknown_event_types_parse_but_are_not_handled(self) -> None:
        event = parse_event({"event": "customer.identification.failed", "data": {"id": 7}})
        assert not event.is_handled


class TestEventKeys:
    def test_the_same_delivery_twice_gives_the_same_key(self) -> None:
        body = factories.charge_success()
        assert parse_event(body).event_key == parse_event(body).event_key

    def test_different_event_types_on_one_reference_do_not_collide(self) -> None:
        charge = parse_event(factories.charge_success("ref_x"))
        refund = parse_event(factories.refund_processed("ref_x"))
        assert charge.event_key != refund.event_key

    def test_a_payload_with_no_ids_falls_back_to_hashing_the_body(self) -> None:
        event = parse_event({"event": "odd.thing", "data": {}}, body=b'{"event":"odd.thing"}')
        assert event.event_key.startswith("odd.thing:body:")


class TestApplyingEvents:
    def test_a_charge_creates_the_transaction(self, session: Session) -> None:
        apply_event(session, parse_event(factories.charge_success()))
        session.commit()
        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert txn.status is TransactionStatus.SUCCESS
        assert txn.amount == Money(125050)
        assert txn.channel is Channel.CARD

    def test_replaying_the_same_event_changes_nothing(self, session: Session) -> None:
        body = factories.charge_success()
        for _ in range(3):
            apply_event(session, parse_event(body))
        session.commit()
        assert session.query(Transaction).count() == 1

    def test_a_dedicated_account_assignment_creates_the_mapping(self, session: Session) -> None:
        apply_event(session, parse_event(factories.dedicated_account_assigned()))
        session.commit()
        account = session.get(DedicatedAccount, "9912345678")
        assert account is not None
        assert account.customer_id == "CUS_ada"
        assert account.customer.name == "Ada Okonkwo"

    def test_an_assignment_missing_its_customer_is_loud(self, session: Session) -> None:
        body = factories.dedicated_account_assigned()
        body["data"]["customer"] = {}
        with pytest.raises(EventError, match="account number and a customer"):
            apply_event(session, parse_event(body))

    def test_a_payment_request_is_recorded(self, session: Session) -> None:
        apply_event(session, parse_event(factories.payment_request()))
        session.commit()
        txn = session.get(Transaction, "ref_inv_1")
        assert txn is not None
        assert txn.stated_reference == "INV-0042"

    def test_a_pending_payment_request_stays_pending(self, session: Session) -> None:
        apply_event(session, parse_event(factories.payment_request(event="paymentrequest.pending")))
        session.commit()
        txn = session.get(Transaction, "ref_inv_1")
        assert txn is not None
        assert txn.status is TransactionStatus.PENDING

    def test_an_outgoing_transfer_is_recorded(self, session: Session) -> None:
        apply_event(session, parse_event(factories.transfer_success()))
        session.commit()
        txn = session.get(Transaction, "ref_out_1")
        assert txn is not None
        assert txn.status is TransactionStatus.SUCCESS


class TestOutOfOrderDelivery:
    """Paystack does not promise order. These are the cases that bite."""

    def test_refund_then_charge_stays_refunded(self, session: Session) -> None:
        apply_event(session, parse_event(factories.refund_processed("ref_card_1", 125050)))
        apply_event(session, parse_event(factories.charge_success("ref_card_1", 125050)))
        session.commit()

        txn = session.get(Transaction, "ref_card_1")
        assert txn is not None
        assert txn.status is TransactionStatus.REFUNDED, "a late success must not un-refund"
        assert txn.refunded_kobo == 125050
        assert txn.amount_kobo == 125050, "the refund must not overwrite the original amount"

    def test_charge_then_refund_ends_refunded_too(self, session: Session) -> None:
        apply_event(session, parse_event(factories.charge_success("ref_card_2", 50_000)))
        apply_event(session, parse_event(factories.refund_processed("ref_card_2", 50_000)))
        session.commit()

        txn = session.get(Transaction, "ref_card_2")
        assert txn is not None
        assert txn.status is TransactionStatus.REFUNDED
        assert txn.net == Money(50_000 - 1976 - 50_000)

    def test_either_order_lands_in_the_same_place(self, session: Session) -> None:
        """The point of the rank rule: the result does not depend on arrival order."""
        forward = [factories.charge_success("ref_a"), factories.refund_processed("ref_a")]
        for body in forward:
            apply_event(session, parse_event(body))
        for body in reversed(forward):
            body = {**body, "data": {**body["data"], "reference": "ref_b"}}
            apply_event(session, parse_event(body))
        session.commit()

        a = session.get(Transaction, "ref_a")
        b = session.get(Transaction, "ref_b")
        assert a is not None and b is not None
        assert a.status == b.status
        assert a.refunded_kobo == b.refunded_kobo

    def test_a_failed_event_cannot_undo_a_success(self, session: Session) -> None:
        apply_event(session, parse_event(factories.charge_success("ref_c")))
        apply_event(session, parse_event(factories.charge_success("ref_c", status="failed")))
        session.commit()
        txn = session.get(Transaction, "ref_c")
        assert txn is not None
        assert txn.status is TransactionStatus.SUCCESS

    def test_a_later_blank_field_does_not_wipe_an_earlier_value(self, session: Session) -> None:
        apply_event(
            session,
            parse_event(factories.bank_transfer("ref_d", narration="TRF/ADA OKONKO/PAYMENT")),
        )
        apply_event(
            session, parse_event(factories.charge_success("ref_d", channel="bank_transfer"))
        )
        session.commit()
        txn = session.get(Transaction, "ref_d")
        assert txn is not None
        assert txn.narration == "TRF/ADA OKONKO/PAYMENT"

    def test_an_event_with_no_reference_is_loud(self, session: Session) -> None:
        with pytest.raises(EventError, match="no reference"):
            apply_event(session, parse_event({"event": "charge.success", "data": {"id": 1}}))
