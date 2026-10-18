from __future__ import annotations

from fastapi.testclient import TestClient

from app import db, main
from app.approval import approval_matches, build_approval_result
from app.schemas import InvoiceData


def configure_temp_db(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "approval-data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "PDF_DIR", data_dir / "invoices")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "taskpilot.db")


def create_pending_task() -> str:
    task_id = db.create_task("Record invoice FAB-2026-022 from Fabrikam Components")
    invoice = InvoiceData(
        vendor="Fabrikam Components",
        invoice_number="FAB-2026-022",
        issue_date="2026-10-17",
        due_date="2026-11-16",
        amount="1275.40",
        currency="USD",
    )
    proposal = build_approval_result(invoice, "src-fabrikam-022", "http://127.0.0.1:8000")
    db.set_task_status(task_id, "pending_approval", proposal)
    db.add_event(task_id, "approval_requested", "Human approval is required.", proposal["proposed_action"])
    return task_id


def test_approval_panel_is_shown_only_for_pending_proposal(tmp_path, monkeypatch) -> None:
    configure_temp_db(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        task_id = create_pending_task()
        response = client.get(f"/history/{task_id}")

    assert response.status_code == 200
    assert "Human approval required" in response.text
    assert "Approve and submit invoice" in response.text
    assert "Reject — do not write" in response.text
    assert "has <strong>not submitted the finance write</strong>" in response.text


def test_approve_persists_exact_proposal_and_schedules_worker(tmp_path, monkeypatch) -> None:
    configure_temp_db(tmp_path, monkeypatch)
    scheduled: list[str] = []

    async def fake_run_task(task_id: str) -> None:
        scheduled.append(task_id)

    monkeypatch.setattr(main, "run_task", fake_run_task)
    with TestClient(main.app) as client:
        task_id = create_pending_task()
        before = db.get_task(task_id)
        proposal = before["result"]["proposed_action"]

        response = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "approve"},
        )
        task = db.get_task(task_id)

    assert response.status_code == 202
    assert response.json()["decision"] == "approved"
    assert task["status"] == "queued"
    assert task["approval"]["decision"] == "approved"
    assert task["approval"]["source_invoice_id"] == proposal["source_invoice_id"]
    assert task["approval"]["invoice"] == proposal["invoice"]
    assert task["result"] is None
    assert scheduled == [task_id]
    assert any(event["kind"] == "approval_granted" for event in task["events"])


def test_reject_is_terminal_and_never_schedules_worker(tmp_path, monkeypatch) -> None:
    configure_temp_db(tmp_path, monkeypatch)
    scheduled: list[str] = []

    async def fake_run_task(task_id: str) -> None:
        scheduled.append(task_id)

    monkeypatch.setattr(main, "run_task", fake_run_task)
    with TestClient(main.app) as client:
        task_id = create_pending_task()
        response = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "reject"},
        )
        task = db.get_task(task_id)
        replay = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "approve"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "rejected"
    assert task["status"] == "rejected"
    assert task["approval"]["decision"] == "rejected"
    assert task["result"]["evidence"]["write_performed"] is False
    assert "did not submit" in task["result"]["message"]
    assert scheduled == []
    assert replay.status_code == 409
    assert any(event["kind"] == "approval_rejected" for event in task["events"])


def test_approval_endpoint_rejects_invalid_or_non_pending_requests(tmp_path, monkeypatch) -> None:
    configure_temp_db(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        task_id = create_pending_task()
        invalid = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "maybe"},
        )
        missing_body_field = client.post(
            f"/api/tasks/{task_id}/approval",
            json={},
        )
        valid_reject = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "reject"},
        )
        resolved = client.post(
            f"/api/tasks/{task_id}/approval",
            json={"decision": "reject"},
        )
        missing_task = client.post(
            "/api/tasks/not-a-task/approval",
            json={"decision": "approve"},
        )

    assert invalid.status_code == 422
    assert missing_body_field.status_code == 422
    assert valid_reject.status_code == 202
    assert resolved.status_code == 409
    assert missing_task.status_code == 404


def test_approval_must_match_invoice_fields_and_source_id() -> None:
    invoice = InvoiceData(
        vendor="Fabrikam Components",
        invoice_number="FAB-2026-022",
        issue_date="2026-10-17",
        due_date="2026-11-16",
        amount="1275.40",
        currency="USD",
    )
    approval = {
        "decision": "approved",
        "source_invoice_id": "src-fabrikam-022",
        "invoice": invoice.model_dump(mode="json"),
    }

    assert approval_matches(approval, invoice, "src-fabrikam-022") is True
    assert approval_matches(approval, invoice, "src-other-999") is False

    altered_invoice = invoice.model_copy(update={"amount": invoice.amount + 1})
    assert approval_matches(approval, altered_invoice, "src-fabrikam-022") is False
    assert approval_matches({**approval, "decision": "rejected"}, invoice, "src-fabrikam-022") is False


def test_langgraph_write_node_pauses_before_touching_browser() -> None:
    import asyncio
    from types import SimpleNamespace

    from app.agent_graph import save_invoice

    invoice = InvoiceData(
        vendor="Fabrikam Components",
        invoice_number="FAB-2026-022",
        issue_date="2026-10-17",
        due_date="2026-11-16",
        amount="1275.40",
        currency="USD",
    )
    # If the gate is working, the node returns before attempting any browser call.
    runtime = SimpleNamespace(
        context={"page": object(), "base_url": "http://127.0.0.1:8000"}
    )
    result = asyncio.run(
        save_invoice(
            {
                "task_request": "Record invoice FAB-2026-022 from Fabrikam Components",
                "task_id": "approval-gate-test",
                "status": "running",
                "events": [],
                "extracted_invoice": invoice.model_dump(mode="json"),
                "selected_invoice_id": "src-fabrikam-022",
            },
            runtime,
        )
    )

    assert result["status"] == "pending_approval"
    assert result["write_performed"] is False
    assert result["result"]["proposed_action"]["source_invoice_id"] == "src-fabrikam-022"
    assert result["result"]["proposed_action"]["invoice"] == invoice.model_dump(mode="json")


def test_langgraph_write_node_blocks_stale_or_mismatched_approval() -> None:
    import asyncio
    from types import SimpleNamespace

    from app.agent_graph import save_invoice

    invoice = InvoiceData(
        vendor="Fabrikam Components",
        invoice_number="FAB-2026-022",
        issue_date="2026-10-17",
        due_date="2026-11-16",
        amount="1275.40",
        currency="USD",
    )
    stale_approval = {
        "decision": "approved",
        "source_invoice_id": "src-fabrikam-022",
        "invoice": {**invoice.model_dump(mode="json"), "amount": "1.00"},
    }
    result = asyncio.run(
        save_invoice(
            {
                "task_request": "Record invoice FAB-2026-022 from Fabrikam Components",
                "status": "running",
                "events": [],
                "approval": stale_approval,
                "extracted_invoice": invoice.model_dump(mode="json"),
                "selected_invoice_id": "src-fabrikam-022",
            },
            SimpleNamespace(
                context={"page": object(), "base_url": "http://127.0.0.1:8000"}
            ),
        )
    )

    assert result["status"] == "failed"
    assert result["result"]["error_type"] == "ApprovalMismatch"


def test_existing_database_is_migrated_with_approval_column(tmp_path, monkeypatch) -> None:
    import sqlite3

    configure_temp_db(tmp_path, monkeypatch)
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db.DB_PATH)
    connection.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            request TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result_json TEXT
        )"""
    )
    connection.commit()
    connection.close()

    db.init_db()

    with sqlite3.connect(db.DB_PATH) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    assert "approval_json" in columns
