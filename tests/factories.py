"""Webhook bodies shaped the way Paystack actually shapes them."""

from __future__ import annotations

from typing import Any


def charge_success(
    reference: str = "ref_card_1",
    amount_kobo: int = 125050,
    *,
    channel: str = "card",
    fees_kobo: int = 1976,
    paid_at: str = "2024-05-17T18:30:00.000Z",
    customer_code: str = "CUS_ada",
    first_name: str = "Ada",
    last_name: str = "Okonkwo",
    metadata: dict[str, Any] | None = None,
    authorization: dict[str, Any] | None = None,
    status: str = "success",
) -> dict[str, Any]:
    return {
        "event": "charge.success",
        "data": {
            "id": abs(hash(reference)) % 10_000_000,
            "domain": "test",
            "status": status,
            "reference": reference,
            "amount": amount_kobo,
            "fees": fees_kobo,
            "paid_at": paid_at,
            "created_at": paid_at,
            "channel": channel,
            "currency": "NGN",
            "metadata": metadata if metadata is not None else {},
            "customer": {
                "id": 1,
                "customer_code": customer_code,
                "first_name": first_name,
                "last_name": last_name,
                "email": f"{first_name}.{last_name}@example.com".lower(),
            },
            "authorization": authorization or {"channel": channel},
        },
    }


def dva_transfer(
    reference: str = "ref_dva_1",
    amount_kobo: int = 250_000,
    account_number: str = "9912345678",
    sender_name: str = "ADA OKONKWO",
    paid_at: str = "2024-05-17T09:05:00.000Z",
) -> dict[str, Any]:
    """Money landing in a customer's dedicated virtual account."""
    body = charge_success(
        reference=reference,
        amount_kobo=amount_kobo,
        channel="dedicated_nuban",
        fees_kobo=5000,
        paid_at=paid_at,
        authorization={
            "channel": "dedicated_nuban",
            "sender_name": sender_name,
            "sender_bank": "GTBank",
            "receiver_bank_account_number": account_number,
            "narration": f"TRF FROM {sender_name}",
        },
    )
    return body


def bank_transfer(
    reference: str = "ref_trf_1",
    amount_kobo: int = 180_000,
    narration: str = "TRF/ADA OKONKO/PAYMENT",
    sender_name: str = "OKONKWO ADA",
    paid_at: str = "2024-05-17T11:00:00.000Z",
) -> dict[str, Any]:
    """Money into the main account. All we get is the narration string."""
    return charge_success(
        reference=reference,
        amount_kobo=amount_kobo,
        channel="bank_transfer",
        fees_kobo=1000,
        paid_at=paid_at,
        authorization={
            "channel": "bank_transfer",
            "sender_name": sender_name,
            "narration": narration,
        },
    )


def refund_processed(
    reference: str = "ref_card_1",
    amount_kobo: int = 125050,
    processed_at: str = "2024-05-18T10:00:00.000Z",
) -> dict[str, Any]:
    return {
        "event": "refund.processed",
        "data": {
            "id": abs(hash("refund" + reference)) % 10_000_000,
            "status": "processed",
            "transaction_reference": reference,
            "reference": reference,
            "amount": amount_kobo,
            "currency": "NGN",
            "created_at": processed_at,
            "channel": "card",
        },
    }


def dedicated_account_assigned(
    account_number: str = "9912345678",
    customer_code: str = "CUS_ada",
    first_name: str = "Ada",
    last_name: str = "Okonkwo",
    bank: str = "Wema Bank",
    assigned_at: str = "2024-05-01T08:00:00.000Z",
) -> dict[str, Any]:
    return {
        "event": "dedicatedaccount.assign.success",
        "data": {
            "customer": {
                "customer_code": customer_code,
                "first_name": first_name,
                "last_name": last_name,
            },
            "dedicated_account": {
                "account_number": account_number,
                "account_name": f"{first_name} {last_name}",
                "bank": {"name": bank},
            },
            "created_at": assigned_at,
        },
    }


def payment_request(
    event: str = "paymentrequest.success",
    reference: str = "ref_inv_1",
    amount_kobo: int = 450_000,
    at: str = "2024-05-17T14:00:00.000Z",
    order_reference: str = "INV-0042",
) -> dict[str, Any]:
    return {
        "event": event,
        "data": {
            "id": abs(hash(reference)) % 10_000_000,
            "status": "success" if event.endswith("success") else "pending",
            "reference": reference,
            "amount": amount_kobo,
            "paid_at": at,
            "created_at": at,
            "currency": "NGN",
            "description": f"Invoice {order_reference}",
            "metadata": {"order_reference": order_reference},
            "customer": {"customer_code": "CUS_ada", "first_name": "Ada", "last_name": "Okonkwo"},
        },
    }


def transfer_success(
    reference: str = "ref_out_1", amount_kobo: int = 90_000, at: str = "2024-05-17T16:00:00.000Z"
) -> dict[str, Any]:
    """Money the business sent out. Recorded, never matched to an order."""
    return {
        "event": "transfer.success",
        "data": {
            "id": abs(hash(reference)) % 10_000_000,
            "status": "success",
            "reference": reference,
            "amount": amount_kobo,
            "created_at": at,
            "reason": "Supplier payout",
            "currency": "NGN",
        },
    }
