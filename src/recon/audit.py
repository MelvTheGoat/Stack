"""The append-only log of every decision the system made.

One function, `record`. It inserts. It never updates, never deletes. If you
find yourself wanting to correct an audit row, write a new one instead.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from recon.enums import Layer
from recon.models import AuditRecord


def record(
    session: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: str,
    layer: Layer | None = None,
    confidence: float | None = None,
    actor: str = "system",
    inputs: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> AuditRecord:
    """Write one audit row.

    `actor` is "system" for anything a layer decided on its own, and the
    reviewer's name for anything a person decided.
    """
    entry = AuditRecord(
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        layer=layer,
        confidence=confidence,
        actor=actor,
        inputs_json=json.dumps(inputs or {}, default=str, sort_keys=True),
        evidence_json=json.dumps(evidence or {}, default=str, sort_keys=True),
    )
    session.add(entry)
    return entry


def history(session: Session, subject_type: str, subject_id: str) -> list[AuditRecord]:
    """Everything that ever happened to one transaction or order, oldest first."""
    return list(
        session.query(AuditRecord)
        .filter(AuditRecord.subject_type == subject_type, AuditRecord.subject_id == subject_id)
        .order_by(AuditRecord.at, AuditRecord.id)
        .all()
    )
