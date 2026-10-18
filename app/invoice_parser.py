from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader

from app.schemas import InvoiceData

FIELD_PATTERNS = {
    "vendor": re.compile(r"^Vendor:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "invoice_number": re.compile(r"^Invoice Number:\s*([A-Z0-9-]+)\s*$", re.MULTILINE | re.IGNORECASE),
    "issue_date": re.compile(r"^Issue Date:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE | re.IGNORECASE),
    "due_date": re.compile(r"^Due Date:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE | re.IGNORECASE),
    "amount": re.compile(
        r"^Amount Due:\s*([A-Z]{3})\s+([\d,]+\.\d{2})\s*$", re.MULTILINE | re.IGNORECASE
    ),
}


class InvoiceExtractionError(ValueError):
    """The document did not contain the required invoice fields."""


def extract_invoice(pdf_path: str | Path) -> InvoiceData:
    try:
        reader = PdfReader(str(pdf_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf raises several format-specific exceptions
        raise InvoiceExtractionError(f"Could not read invoice PDF: {exc}") from exc

    found: dict[str, str] = {}
    missing: list[str] = []
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            missing.append(field)
        elif field == "amount":
            found["currency"] = match.group(1).upper()
            try:
                found["amount"] = str(Decimal(match.group(2).replace(",", "")))
            except InvalidOperation as exc:
                raise InvoiceExtractionError("The invoice amount was not a valid decimal") from exc
        else:
            found[field] = match.group(1).strip()

    if missing:
        raise InvoiceExtractionError(f"Required invoice fields not found: {', '.join(missing)}")

    try:
        return InvoiceData(**found)
    except Exception as exc:
        raise InvoiceExtractionError(f"Extracted invoice fields failed validation: {exc}") from exc
