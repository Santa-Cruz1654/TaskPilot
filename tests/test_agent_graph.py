
import asyncio

from app.agent_graph import taskpilot_graph
from app.planner import PlannerDecision, PlannerError


def run_graph(state):
    return asyncio.run(taskpilot_graph.ainvoke(state))


def initial_state(**overrides):
    state = {
        "task_id": "test-task-001",
        "task_request": "Record invoice INV-001 from Acme Supplies",
        "observed_invoices": [
            {
                "source_invoice_id": "inv-001",
                "vendor": "Acme Supplies",
                "invoice_number": "INV-001",
            }
        ],
        "status": "pending",
        "events": [],
    }
    state.update(overrides)
    return state


def test_empty_task_fails_without_calling_planner(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("Planner should not be called")

    monkeypatch.setattr(
        "app.agent_graph.plan_invoice_selection",
        unexpected_call,
    )

    result = run_graph(initial_state(task_request=""))

    assert result["status"] == "failed"
    assert result["error"] == "Task request cannot be empty."


def test_graph_records_valid_invoice_selection(monkeypatch):
    async def fake_planner(task_request, observed_invoices):
        assert "INV-001" in task_request
        assert observed_invoices[0]["source_invoice_id"] == "inv-001"

        return PlannerDecision(
            action="select_invoice",
            source_invoice_id="inv-001",
            clarification_question=None,
            rationale="The invoice number matches the request.",
        )

    monkeypatch.setattr(
        "app.agent_graph.plan_invoice_selection",
        fake_planner,
    )

    result = run_graph(initial_state())

    assert result["status"] == "running"
    assert result["selected_invoice_id"] == "inv-001"
    assert result["clarification_question"] is None
    assert result["current_step"] == "planning_complete"


def test_graph_requests_clarification(monkeypatch):
    async def fake_planner(task_request, observed_invoices):
        return PlannerDecision(
            action="needs_input",
            source_invoice_id=None,
            clarification_question="Which invoice should I record?",
            rationale="The request does not identify a unique invoice.",
        )

    monkeypatch.setattr(
        "app.agent_graph.plan_invoice_selection",
        fake_planner,
    )

    result = run_graph(initial_state())

    assert result["status"] == "needs_input"
    assert result["selected_invoice_id"] is None
    assert result["clarification_question"] == (
        "Which invoice should I record?"
    )


def test_graph_handles_planner_error(monkeypatch):
    async def fake_planner(task_request, observed_invoices):
        raise PlannerError("Planner service unavailable")

    monkeypatch.setattr(
        "app.agent_graph.plan_invoice_selection",
        fake_planner,
    )

    result = run_graph(initial_state())

    assert result["status"] == "failed"
    assert result["error"] == "Planner service unavailable"
    assert result["current_step"] == "plan_invoice"
