from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.invoice_parser import InvoiceExtractionError, extract_invoice


def make_pdf(path: Path, lines: list[str]) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    y = 730
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 24
    pdf.save()


def test_extract_invoice_fields_and_normalise_amount(tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    make_pdf(
        path,
        [
            "Vendor: Acme Supplies",
            "Invoice Number: INV-2026-016",
            "Issue Date: 2026-10-14",
            "Due Date: 2026-11-13",
            "Amount Due: USD 1,240.50",
        ],
    )

    invoice = extract_invoice(path)

    assert invoice.vendor == "Acme Supplies"
    assert invoice.invoice_number == "INV-2026-016"
    assert invoice.issue_date == "2026-10-14"
    assert invoice.due_date == "2026-11-13"
    assert str(invoice.amount) == "1240.50"
    assert invoice.currency == "USD"


def test_extraction_fails_closed_when_required_field_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.pdf"
    make_pdf(
        path,
        [
            "Vendor: Acme Supplies",
            "Invoice Number: INV-2026-016",
            "Issue Date: 2026-10-14",
            # Missing due date and amount on purpose.
        ],
    )

    with pytest.raises(InvoiceExtractionError, match="Required invoice fields not found"):
        extract_invoice(path)


def test_invoice_data_rejects_due_date_before_issue_date() -> None:
    from app.schemas import InvoiceData

    with pytest.raises(ValueError, match="due_date cannot be earlier"):
        InvoiceData(
            vendor="Acme Supplies",
            invoice_number="INV-2026-TEST",
            issue_date="2026-10-14",
            due_date="2026-10-01",
            amount="10.00",
            currency="USD",
        )
