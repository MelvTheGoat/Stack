"""The small closed sets of words the system is allowed to use."""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    """How the money arrived."""

    CARD = "card"
    """Paid online with a card. Paystack knows exactly which order it was for."""

    DVA = "dva"
    """Transfer into a dedicated virtual account that belongs to one customer."""

    BANK_TRANSFER = "bank_transfer"
    """Transfer into the business's main account. All we get is a narration."""

    CASH = "cash"
    """Handed over in person. A human typed it in. Nothing verifies it."""

    UNKNOWN = "unknown"


class TransactionStatus(StrEnum):
    """Where a payment has got to.

    The order here matters: see `STATUS_RANK`. Webhooks arrive out of order, so
    a payment only ever moves *forward* through this list.
    """

    PENDING = "pending"
    FAILED = "failed"
    SUCCESS = "success"
    REVERSED = "reversed"
    REFUNDED = "refunded"


#: A payment may only move to a status with a higher rank. If `refund.processed`
#: turns up before `charge.success` (which happens), the refund still sticks and
#: the late success does not undo it.
STATUS_RANK: dict[TransactionStatus, int] = {
    TransactionStatus.PENDING: 0,
    TransactionStatus.FAILED: 10,
    TransactionStatus.SUCCESS: 20,
    TransactionStatus.REVERSED: 30,
    TransactionStatus.REFUNDED: 40,
}


class OrderStatus(StrEnum):
    OPEN = "open"
    PART_PAID = "part_paid"
    PAID = "paid"
    OVERPAID = "overpaid"
    VOID = "void"


class Layer(StrEnum):
    """Which layer of the system resolved a case.

    The house rule is deterministic first, probabilistic second, generative
    last. Every resolution records which one decided, so we can report the mix
    for any given day instead of guessing at it.
    """

    EXACT_REFERENCE = "exact_reference"
    DVA_ATTRIBUTION = "dva_attribution"
    AMOUNT_WINDOW = "amount_window"
    PROBABILISTIC = "probabilistic"
    EXTRACTION = "extraction"
    HUMAN = "human"
    UNRESOLVED = "unresolved"


DETERMINISTIC_LAYERS: frozenset[Layer] = frozenset(
    {Layer.EXACT_REFERENCE, Layer.DVA_ATTRIBUTION, Layer.AMOUNT_WINDOW}
)


class DecisionStatus(StrEnum):
    AUTO_CLEARED = "auto_cleared"
    """Confident enough to close the books on without a human looking."""

    QUEUED = "queued"
    """Below threshold. A person has to decide."""

    APPROVED = "approved"
    REJECTED = "rejected"


class RejectReason(StrEnum):
    """Why a human said no. These become training labels, so keep the list short."""

    WRONG_CUSTOMER = "wrong_customer"
    WRONG_AMOUNT = "wrong_amount"
    DUPLICATE_PAYMENT = "duplicate_payment"
    NOT_A_PAYMENT = "not_a_payment"
    ALREADY_SETTLED = "already_settled"
    OTHER = "other"
