"""Bringing a business's own records in from a spreadsheet.

Two files, because they come from two places:

- **Invoices.** What the business is owed, from an invoicing tool, a till, or a
  sheet someone keeps.
- **Payments Paystack never saw.** Transfers straight into the main bank
  account, off the bank statement, and cash. Paystack payments arrive on their
  own, by webhook or by pulling them from Paystack.

The rules are the same for both:

- **All or nothing.** One bad row and nothing is written. Every bad row comes
  back with its line number, so one round of fixing gets the whole file in.
- **The same file twice changes nothing.** Invoices are keyed on their number,
  payments on their reference (or, with no reference, on the line itself).
- **Amounts are naira as people write them** ("12,500.50", "₦12,500") and go
  straight to whole kobo, never through a float. Finer than a kobo is an error.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from recon import audit
from recon.enums import Channel, OrderStatus, TransactionStatus
from recon.match.deterministic import canonical_reference
from recon.models import Customer, Order, Transaction
from recon.money import Money, MoneyError

#: Accepted spellings of each column, after lower-casing and turning spaces into
#: underscores. The first spelling is the one the templates use.
INVOICE_COLUMNS: dict[str, tuple[str, ...]] = {
    "reference": ("reference", "invoice", "invoice_number", "invoice_no", "order"),
    "customer": ("customer", "customer_name", "name"),
    "amount": ("amount", "total"),
    "issued": ("issued", "issued_on", "date", "invoice_date"),
    "customer_id": ("customer_id",),
    "email": ("email",),
    "phone": ("phone",),
    "description": ("description", "item", "items"),
    "status": ("status",),
}

INVOICE_REQUIRED = ("reference", "customer", "amount", "issued")

PAYMENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date", "paid_on", "transaction_date", "value_date"),
    "amount": ("amount", "credit"),
    "narration": ("narration", "description", "remarks", "details"),
    "payer": ("payer", "payer_name", "sender"),
    "reference": ("reference", "transaction_reference", "ref"),
    "channel": ("channel",),
    "invoice": ("invoice", "invoice_reference", "order_reference"),
}

PAYMENT_REQUIRED = ("date", "amount")

INVOICE_TEMPLATE = (
    "reference,customer,amount,issued,customer_id,email,phone,description\n"
    "INV-0001,Ada Okonkwo,25000.00,2024-05-17,,ada@example.com,08031234567,Catering deposit\n"
    'INV-0002,Musa Danjuma,"12,500",17/05/2024,,,,Two cartons\n'
)

PAYMENT_TEMPLATE = (
    "date,amount,narration,payer,reference,channel,invoice\n"
    "2024-05-18,25000.00,TRF FROM ADA OKONKWO INV-0001,ADA OKONKWO,FT24139XYZ,transfer,\n"
    "18/05/2024,12500,cash at the shop,Musa Danjuma,,cash,INV-0002\n"
)

_STATUSES: dict[str, OrderStatus] = {
    "": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "unpaid": OrderStatus.OPEN,
    "part_paid": OrderStatus.PART_PAID,
    "partly paid": OrderStatus.PART_PAID,
    "paid": OrderStatus.PAID,
    "void": OrderStatus.VOID,
    "cancelled": OrderStatus.VOID,
}

_CHANNELS: dict[str, Channel] = {
    "": Channel.BANK_TRANSFER,
    "transfer": Channel.BANK_TRANSFER,
    "bank_transfer": Channel.BANK_TRANSFER,
    "bank": Channel.BANK_TRANSFER,
    "cash": Channel.CASH,
}

_DAY_FIRST = ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%d %b %Y", "%d-%b-%Y")


class UnreadableFileError(ValueError):
    """The file as a whole cannot be read. Row problems are collected instead."""


@dataclass
class Imported:
    """What an upload did, in numbers a person can check against their file."""

    kind: str
    added: int = 0
    updated: int = 0
    already_there: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        if not self.ok:
            return f"Nothing was imported. Fix {len(self.problems)} problem(s) and upload again."
        parts = [f"{self.added} added"]
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.already_there:
            parts.append(f"{self.already_there} already here")
        if self.skipped:
            parts.append(f"{self.skipped} skipped (money going out)")
        return f"{self.kind.capitalize()}: " + ", ".join(parts) + "."


# ------------------------------------------------------------------ reading


def read_rows(
    data: bytes, columns: dict[str, tuple[str, ...]], required: tuple[str, ...]
) -> tuple[list[dict[str, str]], str]:
    """Rows keyed by our column names, and which spelling the amount came under.

    Raises `UnreadableFileError` when the file itself is unreadable or a required
    column is missing, because then there are no rows worth reporting on.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnreadableFileError(
            "this file is not UTF-8 text. In Excel, use Save As and pick 'CSV UTF-8'."
        ) from exc

    first_line = text.split("\n", 1)[0]
    delimiter = ";" if ";" in first_line and "," not in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise UnreadableFileError("the file is empty")

    found: dict[str, str] = {}
    headers = {_header(name): name for name in reader.fieldnames if name}
    for ours, spellings in columns.items():
        for spelling in spellings:
            if spelling in headers:
                found[ours] = headers[spelling]
                break

    missing = [name for name in required if name not in found]
    if missing:
        raise UnreadableFileError(
            f"missing column(s): {', '.join(missing)}. "
            f"The first row should name the columns, e.g. {', '.join(required)}."
        )

    rows = [
        {ours: (raw.get(theirs) or "").strip() for ours, theirs in found.items()} for raw in reader
    ]
    return rows, _header(found["amount"])


def _header(name: str) -> str:
    return "_".join(name.strip().lower().split())


def parse_amount(text: str) -> Money:
    cleaned = text.strip().replace("₦", "").replace(" ", "")
    if cleaned.upper().startswith("NGN"):
        cleaned = cleaned[3:]
    elif re.match(r"^[Nn]\d", cleaned):
        cleaned = cleaned[1:]
    if not cleaned:
        raise MoneyError("no amount")
    amount = Money.from_naira(cleaned)
    if amount.kobo <= 0:
        raise MoneyError(f"{text!r} is not more than zero")
    return amount


def parse_when(text: str) -> datetime:
    """A date or a date and time, as a spreadsheet is likely to write it.

    Slashed dates are day first (17/05/2024), because that is how they are
    written here. A value with no time zone is taken as UTC, which keeps the
    day exactly as written, and the day is what the report is grouped by.
    """
    value = text.strip()
    if not value:
        raise ValueError("no date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for pattern in _DAY_FIRST:
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"{value!r} is not a date (try 2024-05-17 or 17/05/2024)") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


# ----------------------------------------------------------------- invoices


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    reference: str
    customer: str
    customer_id: str
    amount: Money
    issued: datetime
    status: OrderStatus
    email: str = ""
    phone: str = ""
    description: str = ""


def import_invoices(session: Session, data: bytes, *, who: str = "system") -> Imported:
    result = Imported(kind="invoices")
    try:
        rows, _ = read_rows(data, INVOICE_COLUMNS, INVOICE_REQUIRED)
    except UnreadableFileError as exc:
        result.problems.append(str(exc))
        return result

    lines: list[InvoiceLine] = []
    first_seen: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        try:
            line = _invoice(row)
        except (ValueError, MoneyError) as exc:
            result.problems.append(f"line {number}: {exc}")
            continue
        if line.reference in first_seen:
            result.problems.append(
                f"line {number}: {line.reference} is also on line {first_seen[line.reference]}"
            )
            continue
        first_seen[line.reference] = number
        lines.append(line)

    if result.problems:
        return result

    for line in lines:
        customer = session.get(Customer, line.customer_id)
        if customer is None:
            session.add(
                Customer(
                    id=line.customer_id,
                    name=line.customer,
                    email=line.email or None,
                    phone=line.phone or None,
                )
            )
            # So the next invoice for the same customer finds this one.
            session.flush()
        else:
            customer.name = line.customer
            customer.email = line.email or customer.email
            customer.phone = line.phone or customer.phone

        order = session.get(Order, line.reference)
        if order is None:
            session.add(
                Order(
                    reference=line.reference,
                    customer_id=line.customer_id,
                    amount_kobo=line.amount.kobo,
                    issued_at=line.issued,
                    status=line.status,
                    description=line.description,
                )
            )
            result.added += 1
            continue

        before = (order.customer_id, order.amount_kobo, order.issued_at, order.status)
        order.customer_id = line.customer_id
        order.amount_kobo = line.amount.kobo
        order.issued_at = line.issued
        order.status = line.status
        order.description = line.description or order.description
        if before == (order.customer_id, order.amount_kobo, order.issued_at, order.status):
            result.already_there += 1
        else:
            result.updated += 1

    _record(session, result, rows=len(lines), who=who)
    return result


def _invoice(row: dict[str, str]) -> InvoiceLine:
    reference = canonical_reference(row.get("reference", ""))
    if not reference:
        raise ValueError("no invoice number")
    if len(reference) > 64:
        raise ValueError(f"invoice number {reference[:20]}... is longer than 64 characters")
    customer = " ".join(row.get("customer", "").split())
    if not customer:
        raise ValueError(f"{reference} has no customer")
    try:
        amount = parse_amount(row.get("amount", ""))
    except MoneyError as exc:
        raise ValueError(f"{reference}: amount {row.get('amount')!r}: {exc}") from exc
    status_word = row.get("status", "").lower().replace("-", "_")
    if status_word not in _STATUSES:
        raise ValueError(f"{reference}: status {row.get('status')!r} is not open, paid or void")
    customer_id = row.get("customer_id", "") or customer_key(customer)
    if len(customer_id) > 64:
        raise ValueError(f"{reference}: customer_id is longer than 64 characters")
    return InvoiceLine(
        reference=reference,
        customer=customer[:255],
        customer_id=customer_id,
        amount=amount,
        issued=parse_when(row.get("issued", "")),
        status=_STATUSES[status_word],
        email=row.get("email", "")[:255],
        phone=row.get("phone", "")[:32],
        description=row.get("description", "")[:255],
    )


def customer_key(name: str) -> str:
    """A customer id made from a name, for files that do not carry one.

    Two different customers with the same name become one customer this way.
    A `customer_id` column is the fix, and the setup page says so.
    """
    slug = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")
    return f"C-{slug}"[:64]


# ----------------------------------------------------------------- payments


@dataclass(frozen=True, slots=True)
class PaymentLine:
    paid_at: datetime
    amount: Money
    channel: Channel
    narration: str
    payer: str
    reference: str | None
    invoice: str | None
    raw: dict[str, str]

    def fingerprint(self) -> str:
        return "|".join(
            [self.paid_at.isoformat(), str(self.amount.kobo), self.narration, self.payer]
        )


def import_payments(session: Session, data: bytes, *, who: str = "system") -> Imported:
    result = Imported(kind="payments")
    try:
        rows, amount_column = read_rows(data, PAYMENT_COLUMNS, PAYMENT_REQUIRED)
    except UnreadableFileError as exc:
        result.problems.append(str(exc))
        return result

    lines: list[tuple[str, PaymentLine]] = []
    first_seen: dict[str, int] = {}
    repeats: Counter[str] = Counter()
    for number, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        if amount_column == "credit" and _is_blank_amount(row.get("amount", "")):
            # A statement's debit lines: money going out, nothing to match.
            result.skipped += 1
            continue
        try:
            line = _payment(row)
        except (ValueError, MoneyError) as exc:
            result.problems.append(f"line {number}: {exc}")
            continue

        if line.reference is None:
            # No reference on the line, so the line itself is the key. Two
            # identical lines are two payments (that is what a double transfer
            # looks like), so each repeat gets its own number.
            fingerprint = line.fingerprint()
            repeats[fingerprint] += 1
            digest = hashlib.sha256(f"{fingerprint}|{repeats[fingerprint]}".encode()).hexdigest()
            key = ("cash_" if line.channel is Channel.CASH else "stmt_") + digest[:20]
        elif line.reference in first_seen:
            result.problems.append(
                f"line {number}: reference {line.reference} is also on line "
                f"{first_seen[line.reference]}"
            )
            continue
        else:
            key = line.reference
        first_seen[key] = number
        lines.append((key, line))

    if result.problems:
        return result

    for key, line in lines:
        if session.get(Transaction, key) is not None:
            # Never overwrite: the row may have come from Paystack, which knows
            # more about it than a spreadsheet does.
            result.already_there += 1
            continue
        session.add(
            Transaction(
                reference=key,
                channel=line.channel,
                status=TransactionStatus.SUCCESS,
                amount_kobo=line.amount.kobo,
                fees_kobo=0,
                refunded_kobo=0,
                currency="NGN",
                paid_at=line.paid_at,
                last_event_at=line.paid_at,
                narration=line.narration,
                payer_name=line.payer,
                stated_reference=line.invoice,
                # A statement line comes from the bank itself. Cash is only
                # ever somebody's word, and stays marked as such.
                verified=line.channel is Channel.BANK_TRANSFER,
                raw_json=json.dumps({"source": "upload", "row": line.raw}, sort_keys=True),
            )
        )
        result.added += 1

    _record(session, result, rows=len(lines), who=who)
    return result


def _is_blank_amount(text: str) -> bool:
    return text.strip() in ("", "0", "0.00", "-")


def _payment(row: dict[str, str]) -> PaymentLine:
    paid_at = parse_when(row.get("date", ""))
    try:
        amount = parse_amount(row.get("amount", ""))
    except MoneyError as exc:
        raise ValueError(f"amount {row.get('amount')!r}: {exc}") from exc
    channel_word = row.get("channel", "").lower()
    if channel_word not in _CHANNELS:
        raise ValueError(
            f"channel {row.get('channel')!r}: this file is for transfers and cash. "
            "Card and dedicated-account payments come in from Paystack."
        )
    reference = row.get("reference", "") or None
    if reference is not None and len(reference) > 128:
        raise ValueError("reference is longer than 128 characters")
    invoice = canonical_reference(row.get("invoice", ""))
    return PaymentLine(
        paid_at=paid_at,
        amount=amount,
        channel=_CHANNELS[channel_word],
        narration=" ".join(row.get("narration", "").split())[:500],
        payer=" ".join(row.get("payer", "").split())[:255],
        reference=reference,
        invoice=invoice[:64] or None,
        raw=row,
    )


def _record(session: Session, result: Imported, *, rows: int, who: str) -> None:
    audit.record(
        session,
        action=f"imported_{result.kind}",
        subject_type="upload",
        subject_id=result.kind,
        actor=who,
        inputs={"rows": rows},
        evidence={
            "added": result.added,
            "updated": result.updated,
            "already_there": result.already_there,
            "skipped": result.skipped,
        },
    )
