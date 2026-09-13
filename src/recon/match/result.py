"""What a matching layer hands back."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from recon.enums import Layer
from recon.money import Money


@dataclass(frozen=True, slots=True)
class Match:
    """One decision about one payment.

    `order_references` being empty is a real answer, not a failure: it means
    "this payment pays none of our invoices", which is the right call for a
    double submission or money from a stranger. "We could not tell" is a
    different thing, and it is `layer=UNRESOLVED` with `needs_human=True`.
    """

    transaction_reference: str
    order_references: tuple[str, ...]
    layer: Layer
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    needs_human: bool = False
    money_at_risk: Money = field(default_factory=Money.zero)

    @property
    def resolved(self) -> bool:
        """True if a layer reached a conclusion without asking a person."""
        return self.layer is not Layer.UNRESOLVED and not self.needs_human

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}")


def unresolved(
    transaction_reference: str, reason: str, money_at_risk: Money, **evidence: Any
) -> Match:
    return Match(
        transaction_reference=transaction_reference,
        order_references=(),
        layer=Layer.UNRESOLVED,
        confidence=0.0,
        evidence={"reason": reason, **evidence},
        needs_human=True,
        money_at_risk=money_at_risk,
    )
