from typing import Any, Literal, TypedDict


class TaskPilotState(TypedDict, total=False):
    """Serializable business state for one TaskPilot invoice-processing run."""

    # Request and planner configuration.
    task_id: str
    task_request: str
    planner_mode: Literal["deterministic", "llm"]
    persist_events: bool

    # Browser observations and validated selection; no live browser objects.
    observed_invoices: list[dict[str, Any]]
    browser_available: bool
    selected_invoice_id: str | None
    selected_invoice: dict[str, Any] | None

    # Planner decision.
    planner_action: Literal["select_invoice", "needs_input"] | None
    planner_rationale: str | None
    clarification_question: str | None

    # Document extraction.
    downloaded_pdf_path: str | None
    extracted_invoice: dict[str, Any] | None

    # Duplicate prevention, approval, and persistence.
    duplicate_found: bool | None
    already_existed: bool | None
    saved_record_id: str | None
    write_performed: bool | None
    approval: dict[str, Any] | None

    # Independent verification.
    verification_passed: bool | None
    verification_checks: dict[str, bool] | None
    verification: dict[str, Any] | None

    # Result and workflow control.
    result: dict[str, Any] | None
    status: Literal[
        "pending",
        "queued",
        "running",
        "pending_approval",
        "needs_input",
        "completed",
        "failed",
        "rejected",
    ]
    current_step: str
    error: str | None

    # In-memory audit mirror; selected nodes also persist events to SQLite.
    events: list[dict[str, Any]]


class TaskPilotContext(TypedDict, total=False):
    """Run-scoped dependencies. Live browser resources stay out of state."""

    page: Any
    base_url: str
    invoice_extractor: Any
