from decimal import Decimal

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.schemas import InvoiceData


def make_invoice():
    return InvoiceData(
        vendor="Acme Supplies",
        invoice_number="INV-2026-016",
        issue_date="2026-10-14",
        due_date="2026-11-13",
        amount=Decimal("985.75"),
        currency="USD",
    )


def finance_form_data():
    return {
        "vendor": "Acme Supplies",
        "invoice_number": "INV-2026-016",
        "issue_date": "2026-10-14",
        "due_date": "2026-11-13",
        "amount": "985.75",
        "currency": "USD",
        "source_invoice_id": "src-acme-016",
    }


def test_finance_form_rejects_duplicate_submission():
    with TestClient(main_module.app) as client:
        first = client.post(
            "/finance/invoices",
            data=finance_form_data(),
        )

        assert first.status_code == 200
        assert "Invoice saved." in first.text

        second = client.post(
            "/finance/invoices",
            data=finance_form_data(),
        )

        assert second.status_code == 409
        assert "Duplicate invoice" in second.text
        assert "No second record was created" in second.text

    records = db.list_finance_invoices()

    assert len(records) == 1
    assert records[0]["invoice_number"] == "INV-2026-016"


def test_task_submission_api_records_task(monkeypatch):
    def fake_run_task(task_id):
        """Avoid launching a real browser in this API test."""
        db.set_task_status(
            task_id,
            "completed",
            {"message": "Test task completed"},
        )

        db.add_event(
            task_id,
            "task_completed",
            "Test task completed",
        )

    monkeypatch.setattr(
        main_module,
        "run_task",
        fake_run_task,
    )

    request_text = (
        "Process the latest invoice from Acme Supplies"
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/api/tasks",
            json={"request": request_text},
        )

        assert response.status_code == 202

        submitted = response.json()

        assert submitted["id"]
        assert submitted["request"] == request_text

        # The background task runs during TestClient's request.
        details = client.get(
            f"/api/tasks/{submitted['id']}"
        )

        assert details.status_code == 200

        task = details.json()

        assert task["status"] == "completed"
        assert (
            task["result"]["message"]
            == "Test task completed"
        )

        assert any(
            event["kind"] == "task_completed"
            for event in task["events"]
        )


def test_read_only_verification_endpoint_returns_saved_record():
    invoice = make_invoice()

    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/finance/invoices/verify",
            params={
                "vendor": "Acme Supplies",
                "invoice_number": "INV-2026-016",
            },
        )

    assert response.status_code == 200

    record = response.json()

    assert record["vendor"] == "Acme Supplies"
    assert record["invoice_number"] == "INV-2026-016"
    assert record["source_invoice_id"] == "src-acme-016"


def test_read_only_verification_endpoint_returns_404_for_missing_record():
    with TestClient(main_module.app) as client:
        response = client.get(
            "/api/finance/invoices/verify",
            params={
                "vendor": "Unknown Vendor",
                "invoice_number": "UNKNOWN-001",
            },
        )

    assert response.status_code == 404