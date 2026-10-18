import sqlite3
from decimal import Decimal

import pytest

from app import db
from app.schemas import InvoiceData


def make_invoice(
    *,
    vendor="Acme Supplies",
    invoice_number="INV-2026-016",
    amount="985.75",
):
    return InvoiceData(
        vendor=vendor,
        invoice_number=invoice_number,
        issue_date="2026-10-14",
        due_date="2026-11-13",
        amount=Decimal(amount),
        currency="USD",
    )


def test_init_db_seeds_synthetic_invoices():
    invoices = db.list_inbox_invoices()

    assert invoices
    assert any(
        row["invoice_number"] == "INV-2026-016"
        and row["vendor"] == "Acme Supplies"
        for row in invoices
    )


def test_seeded_invoice_pdf_exists():
    invoice = db.get_inbox_invoice("src-acme-016")

    assert invoice is not None

    pdf_path = db.PDF_DIR / invoice["pdf_filename"]

    assert pdf_path.is_file()
    assert pdf_path.stat().st_size > 0


def test_insert_and_read_back_finance_invoice():
    invoice = make_invoice()

    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    actual = db.get_finance_invoice(
        "Acme Supplies",
        "INV-2026-016",
    )

    assert actual is not None
    assert actual["vendor"] == invoice.vendor
    assert actual["invoice_number"] == invoice.invoice_number
    assert actual["issue_date"] == invoice.issue_date
    assert actual["due_date"] == invoice.due_date
    assert Decimal(actual["amount"]) == Decimal("985.75")
    assert actual["currency"] == "USD"
    assert actual["source_invoice_id"] == "src-acme-016"
    assert actual["created_at"]


def test_duplicate_finance_invoice_is_rejected_by_database():
    invoice = make_invoice()

    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_finance_invoice(
            invoice,
            "src-acme-016",
        )

    records = db.list_finance_invoices()

    assert len(records) == 1
    assert records[0]["invoice_number"] == "INV-2026-016"


def test_different_invoice_numbers_can_be_saved_for_same_vendor():
    first = make_invoice(
        invoice_number="INV-2026-016",
        amount="985.75",
    )
    second = make_invoice(
        invoice_number="INV-2026-017",
        amount="550.00",
    )

    db.insert_finance_invoice(first, "src-acme-016")
    db.insert_finance_invoice(second, "src-acme-017")

    records = db.list_finance_invoices()

    assert len(records) == 2


def test_task_and_execution_events_persist():
    task_id = db.create_task(
        "Process the latest invoice from Acme Supplies"
    )

    db.set_task_status(task_id, "running")

    db.add_event(
        task_id,
        "observation",
        "Identified Acme Supplies.",
        {"vendor": "Acme Supplies"},
    )

    db.set_task_status(
        task_id,
        "completed",
        {"message": "Task completed"},
    )

    task = db.get_task(task_id)

    assert task is not None
    assert task["status"] == "completed"
    assert task["result"]["message"] == "Task completed"

    observation = next(
        event
        for event in task["events"]
        if event["kind"] == "observation"
    )

    assert observation["message"] == "Identified Acme Supplies."
    assert observation["data"]["vendor"] == "Acme Supplies"


def test_unknown_finance_invoice_returns_none():
    assert (
        db.get_finance_invoice(
            "Unknown Vendor",
            "UNKNOWN-001",
        )
        is None
    )


def test_unknown_task_returns_none():
    assert db.get_task("missing-task-id") is None