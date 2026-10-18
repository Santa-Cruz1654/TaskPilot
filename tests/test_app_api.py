from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app


def configure_temp_db(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "taskpilot-data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "PDF_DIR", data_dir / "invoices")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "taskpilot.db")


def test_sandbox_pages_and_finance_write_verification(tmp_path, monkeypatch) -> None:
    configure_temp_db(tmp_path, monkeypatch)
    with TestClient(app) as client:
        inbox = client.get("/inbox")
        assert inbox.status_code == 200
        assert "Acme Supplies" in inbox.text
        assert "INV-2026-016" in inbox.text

        form = client.get("/finance/new")
        assert form.status_code == 200
        assert 'name="source_invoice_id"' in form.text

        payload = {
            "vendor": "Acme Supplies",
            "invoice_number": "INV-2026-016",
            "issue_date": "2026-10-14",
            "due_date": "2026-11-13",
            "amount": "985.75",
            "currency": "USD",
            "source_invoice_id": "src-acme-016",
        }
        saved = client.post("/finance/invoices", data=payload, follow_redirects=False)
        assert saved.status_code == 303
        assert saved.headers["location"] == "/finance?status=saved"

        verified = client.get(
            "/api/finance/invoices/verify",
            params={"vendor": "Acme Supplies", "invoice_number": "INV-2026-016"},
        )
        assert verified.status_code == 200
        record = verified.json()
        assert record["amount"] == "985.75"
        assert record["due_date"] == "2026-11-13"
        assert record["source_invoice_id"] == "src-acme-016"

        duplicate = client.post("/finance/invoices", data=payload, follow_redirects=False)
        assert duplicate.status_code == 409
        assert "No second record was created" in duplicate.text
