from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from app import db
from app.approval import approval_matches, build_approval_result
from app.agent_state import TaskPilotContext, TaskPilotState
from app.browser import read_visible_invoice_rows
from app.invoice_parser import InvoiceExtractionError, extract_invoice
from app.planner import PlannerError, plan_invoice_selection
from app.schemas import InvoiceData


TERMINAL_STATUSES = {"pending_approval", "needs_input", "completed", "failed", "rejected"}


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def _money(value: str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _row_id(row: dict[str, Any]) -> str:
    """Read the source identifier from current or legacy invoice-row shapes."""
    return str(row.get("id") or row.get("source_invoice_id") or "")


def _with_row_id(row: dict[str, Any]) -> dict[str, Any]:
    """Return a row with the canonical ``id`` field populated."""
    normalized = dict(row)
    normalized["id"] = _row_id(row)
    return normalized


def _append_event(
    state: TaskPilotState,
    *,
    step: str,
    outcome: str,
    message: str,
    kind: str | None = None,
    data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Append an event to state and persist it immediately when task_id exists."""
    event: dict[str, Any] = {"step": step, "result": outcome}
    if data:
        event.update(data)

    task_id = state.get("task_id")
    if task_id and state.get("persist_events", False):
        payload = {"step": step, "result": outcome}
        if data:
            payload.update(data)
        db.add_event(task_id, kind or step, message, payload)

    return [*state.get("events", []), event]


def _failure(
    state: TaskPilotState,
    *,
    step: str,
    message: str,
    error_type: str = "TaskPilotError",
    kind: str = "task_failed",
) -> dict[str, Any]:
    events = _append_event(
        state,
        step=step,
        outcome="failed",
        message=message,
        kind=kind,
        data={"error_type": error_type, "reason": message},
    )
    return {
        "status": "failed",
        "current_step": step,
        "error": message,
        "result": {"message": message, "error_type": error_type},
        "events": events,
    }


def _needs_input(state: TaskPilotState, *, step: str, question: str) -> dict[str, Any]:
    events = _append_event(
        state,
        step=step,
        outcome="needs_input",
        message=question,
        kind="needs_input",
        data={"clarification_question": question},
    )
    return {
        "status": "needs_input",
        "current_step": step,
        "error": None,
        "clarification_question": question,
        "result": {"message": question, "clarification_question": question},
        "events": events,
    }


def _verify_record(
    expected: InvoiceData,
    source_invoice_id: str,
) -> dict[str, Any]:
    """Read a finance record back and compare all eight required fields."""
    actual = db.get_finance_invoice(expected.vendor, expected.invoice_number)
    if actual is None:
        return {
            "passed": False,
            "reason": "No saved finance record was found after submission.",
            "checks": {"record_exists": False},
        }

    checks = {
        "record_exists": True,
        "vendor_matches": actual["vendor"].casefold() == expected.vendor.casefold(),
        "invoice_number_matches": actual["invoice_number"] == expected.invoice_number,
        "issue_date_matches": actual["issue_date"] == expected.issue_date,
        "due_date_matches": actual["due_date"] == expected.due_date,
        "amount_matches": _money(actual["amount"]) == _money(expected.amount),
        "currency_matches": actual["currency"] == expected.currency,
        "source_document_matches": actual["source_invoice_id"] == source_invoice_id,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "record": {
            "vendor": actual["vendor"],
            "invoice_number": actual["invoice_number"],
            "issue_date": actual["issue_date"],
            "due_date": actual["due_date"],
            "amount": actual["amount"],
            "currency": actual["currency"],
            "source_invoice_id": actual["source_invoice_id"],
            "created_at": actual["created_at"],
        },
    }


def _deterministic_plan(
    request: str,
    rows: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str | None]:
    """Return a deterministic candidate or a clarification question."""
    if not any(term in request.casefold() for term in ("invoice", "bill", "accounts payable")):
        return None, (
            "This prototype currently supports invoice-processing tasks. "
            "Please submit an invoice request."
        )

    request_normalised = _normalise(request)
    explicit_invoice = next(
        (
            _with_row_id(row)
            for row in rows
            if row.get("invoice_number", "").casefold() in request_normalised
        ),
        None,
    )
    if explicit_invoice is not None:
        return explicit_invoice, None

    vendors = sorted({row.get("vendor", "") for row in rows if row.get("vendor")})
    matched_vendors = [vendor for vendor in vendors if _normalise(vendor) in request_normalised]

    if len(matched_vendors) > 1:
        return None, (
            f"The request mentions multiple vendors ({', '.join(matched_vendors)}). "
            "Please specify one vendor."
        )
    if len(matched_vendors) == 1:
        vendor_rows = [
            row for row in rows if _normalise(row.get("vendor", "")) == _normalise(matched_vendors[0])
        ]
        if vendor_rows:
            latest = max(vendor_rows, key=lambda row: row.get("issue_date", ""))
            return _with_row_id(latest), None

    available = ", ".join(vendors)
    return None, (
        "I couldn't identify the vendor in your request. "
        f"Available sandbox vendors: {available}. Please name one of them."
    )


def _validate_selection(
    request: str,
    selected_invoice_id: str | None,
    rows: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str | None, str | None]:
    """Validate a proposed selection against the inbox and explicit constraints.

    Return (selected_row, clarification_question, rejection_reason).
    Clarification and invalid model choices are distinct outcomes.
    """
    selected = next(
        (
            _with_row_id(row)
            for row in rows
            if _row_id(row) == selected_invoice_id
        ),
        None,
    )
    if selected is None:
        return None, None, "The planner-selected invoice was not present in the observed inbox."

    request_normalised = _normalise(request)
    explicit_invoice = next(
        (
            _with_row_id(row)
            for row in rows
            if row.get("invoice_number", "").casefold() in request_normalised
        ),
        None,
    )
    if explicit_invoice is not None and _row_id(selected) != _row_id(explicit_invoice):
        return None, None, (
            "The planner selected a different invoice from the invoice number explicitly "
            "specified in the request. The selection was rejected."
        )

    known_vendors = sorted({row.get("vendor", "") for row in rows if row.get("vendor")})
    mentioned_vendors = [vendor for vendor in known_vendors if _normalise(vendor) in request_normalised]
    if len(mentioned_vendors) > 1:
        return None, (
            "The request mentions multiple vendors: "
            + ", ".join(mentioned_vendors)
            + ". Please specify one vendor."
        ), None

    if (
        len(mentioned_vendors) == 1
        and _normalise(selected.get("vendor", "")) != _normalise(mentioned_vendors[0])
    ):
        return None, None, (
            "The planner selected an invoice from a different vendor than the vendor explicitly "
            "named in the request. The selection was rejected."
        )

    return selected, None, None


def validate_task(state: TaskPilotState) -> dict[str, Any]:
    """Validate the request before observing the inbox or invoking the LLM."""
    task_request = state.get("task_request", "").strip()
    if not task_request:
        return _failure(
            state,
            step="validate_task",
            message="Task request cannot be empty.",
            error_type="ValueError",
        )

    # This prototype supports invoice-processing tasks only. Reject generic
    # commands such as "Run task" before observing candidates or calling an LLM.
    normalized_request = _normalise(task_request)
    supported_request = any(
        term in normalized_request
        for term in ("invoice", "bill", "accounts payable")
    )
    if not supported_request:
        return _needs_input(
            state,
            step="validate_task",
            question=(
                "TaskPilot currently supports invoice-processing tasks. "
                "Please specify an invoice request, preferably with a vendor "
                "name or invoice number. For example: 'Record invoice "
                "INV-2026-016 from Acme Supplies'."
            ),
        )

    events = _append_event(
        state,
        step="validate_task",
        outcome="passed",
        message="Task request passed initial validation.",
        kind="task_validated",
    )
    return {
        "task_request": task_request,
        "status": "running",
        "current_step": "validate_task",
        "error": None,
        "events": events,
    }


def route_after_validation(state: TaskPilotState) -> Literal["observe_inbox", "finish"]:
    return "observe_inbox" if state.get("status") == "running" else "finish"


async def observe_inbox(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Observe invoice candidates using the page supplied by the worker."""
    context = runtime.context or {}
    page = context.get("page")

    # The no-page path preserves isolated planning-node unit tests. The worker's
    # production invocation always supplies the live browser page.
    if page is None:
        if "observed_invoices" in state:
            rows = state.get("observed_invoices", [])
            events = _append_event(
                state,
                step="observe_inbox",
                outcome="passed",
                message="Used invoice candidates supplied by the graph caller.",
                kind="observation",
                data={"visible_invoice_count": len(rows), "source": "provided_state"},
            )
            return {
                "observed_invoices": rows,
                "browser_available": False,
                "status": "running",
                "current_step": "observe_inbox",
                "error": None,
                "events": events,
            }
        return _failure(
            state,
            step="observe_inbox",
            message="A Playwright page is required to observe the inbox.",
        )

    base_url = context.get("base_url", "").rstrip("/")
    if not base_url:
        return _failure(
            state,
            step="observe_inbox",
            message="TaskPilot base URL is missing from runtime context.",
            error_type="ValueError",
        )

    try:
        await page.goto(f"{base_url}/inbox", wait_until="domcontentloaded")
        rows = await read_visible_invoice_rows(page)
    except Exception as exc:
        return _failure(
            state,
            step="observe_inbox",
            message=f"Inbox observation failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    events = _append_event(
        state,
        step="observe_inbox",
        outcome="passed",
        message="Inspected the inbox and collected visible invoice candidates.",
        kind="observation",
        data={"visible_invoice_count": len(rows), "source": "playwright"},
    )
    return {
        "observed_invoices": rows,
        "browser_available": True,
        "status": "running",
        "current_step": "observe_inbox",
        "error": None,
        "events": events,
    }


def route_after_observation(state: TaskPilotState) -> Literal["plan_invoice", "finish"]:
    return "plan_invoice" if state.get("status") == "running" else "finish"


async def plan_invoice(state: TaskPilotState) -> dict[str, Any]:
    """Produce a selection proposal; it is not accepted until validation succeeds."""
    rows = state.get("observed_invoices", [])
    request = state.get("task_request", "")
    planner_mode = state.get("planner_mode", "llm")

    try:
        if planner_mode == "deterministic":
            row, question = _deterministic_plan(request, rows)
            if question:
                events = _append_event(
                    state,
                    step="plan_invoice",
                    outcome="needs_input",
                    message=question,
                    kind="needs_input",
                    data={"clarification_question": question},
                )
                return {
                    "planner_action": "needs_input",
                    "selected_invoice_id": None,
                    "planner_rationale": "Deterministic matching could not identify one invoice.",
                    "clarification_question": question,
                    "status": "needs_input",
                    "current_step": "plan_invoice",
                    "error": None,
                    "result": {"message": question, "clarification_question": question},
                    "events": events,
                }
            assert row is not None
            proposed_id = _row_id(row)
            rationale = "Selected by deterministic invoice/vendor matching."
            proposal_kind = "deterministic_proposal"
        elif planner_mode == "llm":
            decision = await plan_invoice_selection(
                task_request=request,
                observed_invoices=rows,
            )
            if decision.action == "needs_input":
                question = decision.clarification_question or (
                    "Could you specify which invoice you would like to record?"
                )
                events = _append_event(
                    state,
                    step="plan_invoice",
                    outcome="needs_input",
                    message=question,
                    kind="needs_input",
                    data={"reason": decision.rationale, "clarification_question": question},
                )
                return {
                    "planner_action": "needs_input",
                    "selected_invoice_id": None,
                    "planner_rationale": decision.rationale,
                    "clarification_question": question,
                    "status": "needs_input",
                    "current_step": "plan_invoice",
                    "error": None,
                    "result": {"message": question, "clarification_question": question},
                    "events": events,
                }
            proposed_id = decision.source_invoice_id
            rationale = decision.rationale
            proposal_kind = "planner_proposal"
        else:
            return _failure(
                state,
                step="plan_invoice",
                message=(
                    "Unsupported TASKPILOT_PLANNER_MODE. Use 'deterministic' or 'llm'."
                ),
                error_type="ValueError",
                kind="planner_failed",
            )
    except PlannerError as exc:
        return _failure(
            state,
            step="plan_invoice",
            message=str(exc),
            error_type="PlannerError",
            kind="planner_failed",
        )
    except Exception as exc:
        return _failure(
            state,
            step="plan_invoice",
            message=f"Planning failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
            kind="planner_failed",
        )

    events = _append_event(
        state,
        step="plan_invoice",
        outcome="proposed",
        message=(
            "The LangGraph planner proposed an invoice selection; application validation is pending."
            if planner_mode == "llm"
            else "The deterministic planner proposed an invoice selection; validation is pending."
        ),
        kind=proposal_kind,
        data={"source_invoice_id": proposed_id, "rationale": rationale},
    )
    return {
        "planner_action": "select_invoice",
        "selected_invoice_id": proposed_id,
        "planner_rationale": rationale,
        "clarification_question": None,
        "status": "running",
        "current_step": "plan_invoice",
        "error": None,
        "events": events,
    }


def route_after_planning(
    state: TaskPilotState,
) -> Literal["validate_selection", "finish"]:
    if state.get("status") != "running":
        return "finish"
    return "validate_selection"


def validate_selection(state: TaskPilotState) -> dict[str, Any]:
    """Validate proposal against observed rows and user-stated constraints."""
    rows = state.get("observed_invoices", [])
    selected, question, rejection = _validate_selection(
        state.get("task_request", ""),
        state.get("selected_invoice_id"),
        rows,
    )
    if question:
        return _needs_input(state, step="validate_selection", question=question)
    if rejection:
        return _failure(
            state,
            step="validate_selection",
            message=rejection,
            error_type="InvalidPlannerSelection",
            kind="planner_failed",
        )
    if selected is None:
        return _failure(
            state,
            step="validate_selection",
            message="The planner did not return a usable invoice selection.",
            error_type="InvalidPlannerSelection",
            kind="planner_failed",
        )

    events = _append_event(
        state,
        step="validate_selection",
        outcome="passed",
        message="The planner selection passed application validation.",
        kind="planner_decision",
        data={
            "source_invoice_id": _row_id(selected),
            "vendor": selected["vendor"],
            "invoice_number": selected["invoice_number"],
            "rationale": state.get("planner_rationale"),
        },
    )
    return {
        "selected_invoice_id": _row_id(selected),
        "selected_invoice": selected,
        "status": "running",
        # Preserve the existing graph's public state contract: once a
        # selection is accepted, planning is complete even though the full
        # workflow continues through downstream execution nodes.
        "current_step": "planning_complete",
        "error": None,
        "events": events,
    }


def route_after_selection(
    state: TaskPilotState,
) -> Literal["resolve_invoice", "finish"]:
    if state.get("status") != "running":
        return "finish"
    # Compatibility for unit tests that exercise planning without a real page.
    if not state.get("browser_available"):
        return "finish"
    return "resolve_invoice"


async def resolve_invoice(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Re-resolve the selected ID in the browser's vendor-filtered inbox."""
    context = runtime.context or {}
    page = context.get("page")
    selected = state.get("selected_invoice")
    if page is None or selected is None:
        return _failure(
            state,
            step="resolve_invoice",
            message="Cannot resolve the selected invoice without the browser page and selection.",
            error_type="RuntimeContextError",
        )

    try:
        search = page.get_by_label("Search invoices")
        await search.fill(selected["vendor"])
        await search.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        filtered_rows = await read_visible_invoice_rows(page)
        candidates = [
            row for row in filtered_rows
            if _row_id(row) == _row_id(selected)
            and _normalise(row.get("vendor", "")) == _normalise(selected.get("vendor", ""))
            and row.get("invoice_number") == selected.get("invoice_number")
        ]
    except Exception as exc:
        return _failure(
            state,
            step="resolve_invoice",
            message=f"Could not resolve the selected invoice: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    if not candidates:
        return _needs_input(
            state,
            step="resolve_invoice",
            question=(
                f"No invoice for {selected['vendor']} matched the validated selection. "
                "No finance record was created. Please review the inbox and try again."
            ),
        )

    selected = candidates[0]
    events = _append_event(
        state,
        step="resolve_invoice",
        outcome="passed",
        message="Resolved the validated invoice selection against the filtered inbox results.",
        kind="plan_action",
        data={
            "source_invoice_id": _row_id(selected),
            "vendor": selected["vendor"],
            "invoice_number": selected["invoice_number"],
            "issue_date": selected["issue_date"],
            "planner_mode": state.get("planner_mode", "llm"),
        },
    )
    return {
        "selected_invoice": selected,
        "selected_invoice_id": _row_id(selected),
        "current_step": "resolve_invoice",
        "events": events,
    }


def route_after_resolve(state: TaskPilotState) -> Literal["download_extract", "finish"]:
    return "download_extract" if state.get("status") == "running" else "finish"


async def download_extract(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Download selected PDF, parse it, and validate it against inbox metadata."""
    context = runtime.context or {}
    page = context.get("page")
    selected = state.get("selected_invoice")
    if page is None or selected is None:
        return _failure(
            state,
            step="download_extract",
            message="Cannot download an invoice without a browser page and validated selection.",
            error_type="RuntimeContextError",
        )

    try:
        async with page.expect_download() as download_info:
            await page.get_by_role(
                "link", name=f"Download {selected['invoice_number']} PDF"
            ).click()
        download = await download_info.value
        pdf_path = await download.path()
        if not pdf_path:
            raise InvoiceExtractionError("The browser did not provide a downloaded invoice file.")
        # The worker supplies this dependency so tests and alternative parsers
        # can inject an extractor without changing graph orchestration.
        invoice_extractor = context.get("invoice_extractor", extract_invoice)
        invoice = invoice_extractor(Path(pdf_path))
    except (InvoiceExtractionError, ValidationError) as exc:
        return _failure(
            state,
            step="download_extract",
            message=str(exc),
            error_type=type(exc).__name__,
        )
    except Exception as exc:
        return _failure(
            state,
            step="download_extract",
            message=f"PDF download or extraction failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    if (
        _normalise(invoice.vendor) != _normalise(selected["vendor"])
        or invoice.invoice_number != selected["invoice_number"]
        or invoice.issue_date != selected["issue_date"]
    ):
        return _failure(
            state,
            step="download_extract",
            message="The downloaded document does not match the invoice row selected in the inbox.",
            error_type="InvoiceSourceMismatch",
        )

    invoice_data = invoice.model_dump(mode="json")
    events = _append_event(
        state,
        step="download_extract",
        outcome="passed",
        message="Downloaded the PDF and extracted validated invoice fields.",
        kind="document_extracted",
        data={
            "vendor": invoice.vendor,
            "invoice_number": invoice.invoice_number,
            "issue_date": invoice.issue_date,
            "due_date": invoice.due_date,
            "amount": str(invoice.amount.quantize(Decimal("0.01"))),
            "currency": invoice.currency,
        },
    )
    return {
        "downloaded_pdf_path": str(pdf_path),
        "extracted_invoice": invoice_data,
        "status": "running",
        "current_step": "download_extract",
        "error": None,
        "events": events,
    }


def route_after_extraction(state: TaskPilotState) -> Literal["check_duplicate", "finish"]:
    return "check_duplicate" if state.get("status") == "running" else "finish"


def check_duplicate(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Avoid duplicate writes; verify an existing record before deciding the outcome."""
    invoice_data = state.get("extracted_invoice")
    source_invoice_id = state.get("selected_invoice_id")
    if not invoice_data or not source_invoice_id:
        return _failure(
            state,
            step="check_duplicate",
            message="Extracted invoice data or source invoice ID is missing.",
            error_type="MissingInvoiceData",
        )

    try:
        invoice = InvoiceData.model_validate(invoice_data)
        existing_record = db.get_finance_invoice(invoice.vendor, invoice.invoice_number)
    except Exception as exc:
        return _failure(
            state,
            step="check_duplicate",
            message=f"Duplicate check failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    context = runtime.context or {}
    base_url = context.get("base_url", os.getenv("TASKPILOT_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")

    if existing_record is None:
        events = _append_event(
            state,
            step="check_duplicate",
            outcome="not_found",
            message="No existing finance record was found for this invoice.",
            kind="duplicate_check",
            data={"invoice_number": invoice.invoice_number, "duplicate_found": False},
        )
        return {
            "duplicate_found": False,
            "already_existed": False,
            "write_performed": False,
            "status": "running",
            "current_step": "check_duplicate",
            "events": events,
        }

    duplicate_event = _append_event(
        state,
        step="check_duplicate",
        outcome="found",
        message="An existing finance record was found. Verifying it before deciding whether to write.",
        kind="duplicate_check",
        data={
            "invoice_number": invoice.invoice_number,
            "existing_source_invoice_id": existing_record["source_invoice_id"],
            "requested_source_invoice_id": source_invoice_id,
        },
    )
    verification = _verify_record(invoice, source_invoice_id)
    verification_event = _append_event(
        {**state, "events": duplicate_event},
        step="verification",
        outcome="passed" if verification["passed"] else "failed",
        message=(
            "Existing finance record matches the source invoice."
            if verification["passed"]
            else "Existing finance record does not match the source invoice."
        ),
        kind="verification",
        data=verification,
    )
    if verification["passed"]:
        result = {
            "message": "Invoice was already recorded and independently verified. No duplicate was created.",
            "invoice": invoice.model_dump(mode="json"),
            "source_invoice_id": source_invoice_id,
            "already_existed": True,
            "verification": verification,
            "evidence": {
                "finance_url": f"{base_url}/finance",
                "verification_checks_passed": sum(verification["checks"].values()),
                "verification_checks_total": len(verification["checks"]),
                "write_performed": False,
            },
        }
        completion_events = _append_event(
            {**state, "events": verification_event},
            step="task_completed",
            outcome="completed",
            message=result["message"],
            kind="task_completed",
            data=result["evidence"],
        )
        return {
            "duplicate_found": True,
            "already_existed": True,
            "write_performed": False,
            "verification_passed": True,
            "verification_checks": verification["checks"],
            "verification": verification,
            "status": "completed",
            "current_step": "check_duplicate",
            "result": result,
            "events": completion_events,
        }

    message = (
        "An existing finance record was found, but it does not match the source invoice. "
        "No duplicate was created. Manual review is required."
    )
    result = {
        "message": message,
        "invoice": invoice.model_dump(mode="json"),
        "source_invoice_id": source_invoice_id,
        "already_existed": True,
        "verification": verification,
        "evidence": {
            "finance_url": f"{base_url}/finance",
            "write_performed": False,
        },
    }
    failed_state = _failure(
        {**state, "events": verification_event},
        step="check_duplicate",
        message=message,
        error_type="ConflictingFinanceRecord",
        kind="task_failed",
    )
    return {
        **failed_state,
        "duplicate_found": True,
        "already_existed": True,
        "write_performed": False,
        "verification_passed": False,
        "verification_checks": verification["checks"],
        "verification": verification,
        "result": result,
        "events": failed_state["events"],
    }


def route_after_duplicate(state: TaskPilotState) -> Literal["save_invoice", "finish"]:
    if state.get("status") != "running":
        return "finish"
    if state.get("duplicate_found") is True:
        return "finish"
    return "save_invoice"


async def save_invoice(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Submit a new invoice through the existing local finance form."""
    context = runtime.context or {}
    page = context.get("page")
    invoice_data = state.get("extracted_invoice")
    source_invoice_id = state.get("selected_invoice_id")
    base_url = context.get("base_url", "").rstrip("/")
    if page is None or not invoice_data or not source_invoice_id or not base_url:
        return _failure(
            state,
            step="save_invoice",
            message="Browser context, invoice fields, source invoice ID, or base URL is missing.",
            error_type="RuntimeContextError",
        )

    try:
        invoice = InvoiceData.model_validate(invoice_data)
    except (ValidationError, TypeError, ValueError) as exc:
        return _failure(
            state,
            step="save_invoice",
            message=f"Invoice data could not be validated before approval: {exc}",
            error_type=type(exc).__name__,
        )

    approval = state.get("approval")
    if not approval:
        proposal = build_approval_result(invoice, source_invoice_id, base_url)
        events = _append_event(
            state,
            step="approval_gate",
            outcome="required",
            message=(
                "A human must approve the proposed finance write. "
                "No finance record has been submitted."
            ),
            kind="approval_requested",
            data={
                "source_invoice_id": source_invoice_id,
                "vendor": invoice.vendor,
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.amount.quantize(Decimal("0.01"))),
                "currency": invoice.currency,
            },
        )
        return {
            "status": "pending_approval",
            "current_step": "approval_gate",
            "error": None,
            "result": proposal,
            "approval_required": True,
            "write_performed": False,
            "events": events,
        }

    if not approval_matches(approval, invoice, source_invoice_id):
        return _failure(
            state,
            step="approval_gate",
            message=(
                "The stored approval does not match the exact invoice proposal "
                "currently observed. The finance write was blocked."
            ),
            error_type="ApprovalMismatch",
            kind="approval_validation_failed",
        )

    try:
        await page.goto(f"{base_url}/finance/new", wait_until="domcontentloaded")
        await page.get_by_label("Vendor").fill(invoice.vendor)
        await page.get_by_label("Invoice number").fill(invoice.invoice_number)
        await page.get_by_label("Issue date").fill(invoice.issue_date)
        await page.get_by_label("Due date").fill(invoice.due_date)
        await page.get_by_label("Amount").fill(f"{invoice.amount:.2f}")
        await page.get_by_label("Currency").select_option(invoice.currency)
        # This value originates from the observed inbox, never from the model.
        await page.locator("input[name='source_invoice_id']").evaluate(
            "(element, value) => { element.value = value; }",
            source_invoice_id,
        )
        await page.get_by_role("button", name="Save invoice").click()
    except Exception as exc:
        return _failure(
            state,
            step="save_invoice",
            message=f"Finance form submission failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    events = _append_event(
        state,
        step="save_invoice",
        outcome="submitted",
        message="Submitted the invoice through the finance application's web form.",
        kind="write_attempted",
        data={
            "invoice_number": invoice.invoice_number,
            "source_invoice_id": source_invoice_id,
        },
    )
    return {
        "write_performed": True,
        "already_existed": False,
        "status": "running",
        "current_step": "save_invoice",
        "events": events,
    }


def route_after_save(state: TaskPilotState) -> Literal["verify_invoice", "finish"]:
    return "verify_invoice" if state.get("status") == "running" else "finish"


def verify_invoice(
    state: TaskPilotState,
    runtime: Runtime[TaskPilotContext],
) -> dict[str, Any]:
    """Independently read back a newly saved record and require all checks to pass."""
    invoice_data = state.get("extracted_invoice")
    source_invoice_id = state.get("selected_invoice_id")
    if not invoice_data or not source_invoice_id:
        return _failure(
            state,
            step="verify_invoice",
            message="Cannot verify a saved record without invoice data and source ID.",
            error_type="MissingInvoiceData",
        )

    try:
        invoice = InvoiceData.model_validate(invoice_data)
        verification = _verify_record(invoice, source_invoice_id)
    except Exception as exc:
        return _failure(
            state,
            step="verify_invoice",
            message=f"Independent verification failed: {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )

    passed = verification["passed"]
    events = _append_event(
        state,
        step="verify_invoice",
        outcome="passed" if passed else "failed",
        message=(
            "Independent read-back verification passed."
            if passed
            else "Independent read-back verification failed."
        ),
        kind="verification",
        data=verification,
    )
    context = runtime.context or {}
    base_url = context.get(
        "base_url", os.getenv("TASKPILOT_BASE_URL", "http://127.0.0.1:8000")
    ).rstrip("/")
    checks = verification.get("checks", {})

    if passed:
        result = {
            "message": "Invoice recorded and independently verified.",
            "invoice": invoice.model_dump(mode="json"),
            "source_invoice_id": source_invoice_id,
            "already_existed": False,
            "verification": verification,
            "evidence": {
                "inbox_url": f"{base_url}/inbox?q={invoice.vendor.replace(' ', '+')}",
                "finance_url": f"{base_url}/finance",
                "verification_checks_passed": sum(checks.values()),
                "verification_checks_total": len(checks),
                "write_performed": True,
            },
        }
        events = _append_event(
            {**state, "events": events},
            step="task_completed",
            outcome="completed",
            message=result["message"],
            kind="task_completed",
            data=result["evidence"],
        )
        return {
            "status": "completed",
            "current_step": "task_completed",
            "error": None,
            "result": result,
            "verification_passed": True,
            "verification_checks": checks,
            "verification": verification,
            "events": events,
        }

    message = "The worker could not verify that the finance record matches the invoice."
    result = {
        "message": message,
        "invoice": invoice.model_dump(mode="json"),
        "source_invoice_id": source_invoice_id,
        "already_existed": False,
        "verification": verification,
        "evidence": {
            "finance_url": f"{base_url}/finance",
            "write_performed": True,
        },
    }
    failure = _failure(
        {**state, "events": events},
        step="verify_invoice",
        message=message,
        error_type="VerificationFailed",
        kind="task_failed",
    )
    return {
        **failure,
        "result": result,
        "verification_passed": False,
        "verification_checks": checks,
        "verification": verification,
    }


def route_after_terminal_check(state: TaskPilotState) -> Literal["finish"]:
    return "finish"


def build_agent_graph():
    """Build the complete TaskPilot invoice workflow graph."""
    graph = StateGraph(TaskPilotState, context_schema=TaskPilotContext)

    graph.add_node("validate_task", validate_task)
    graph.add_node("observe_inbox", observe_inbox)
    graph.add_node("plan_invoice", plan_invoice)
    graph.add_node("validate_selection", validate_selection)
    graph.add_node("resolve_invoice", resolve_invoice)
    graph.add_node("download_extract", download_extract)
    graph.add_node("check_duplicate", check_duplicate)
    graph.add_node("save_invoice", save_invoice)
    graph.add_node("verify_invoice", verify_invoice)

    graph.add_edge(START, "validate_task")
    graph.add_conditional_edges(
        "validate_task",
        route_after_validation,
        {"observe_inbox": "observe_inbox", "finish": END},
    )
    graph.add_conditional_edges(
        "observe_inbox",
        route_after_observation,
        {"plan_invoice": "plan_invoice", "finish": END},
    )
    graph.add_conditional_edges(
        "plan_invoice",
        route_after_planning,
        {"validate_selection": "validate_selection", "finish": END},
    )
    graph.add_conditional_edges(
        "validate_selection",
        route_after_selection,
        {"resolve_invoice": "resolve_invoice", "finish": END},
    )
    graph.add_conditional_edges(
        "resolve_invoice",
        route_after_resolve,
        {"download_extract": "download_extract", "finish": END},
    )
    graph.add_conditional_edges(
        "download_extract",
        route_after_extraction,
        {"check_duplicate": "check_duplicate", "finish": END},
    )
    graph.add_conditional_edges(
        "check_duplicate",
        route_after_duplicate,
        {"save_invoice": "save_invoice", "finish": END},
    )
    graph.add_conditional_edges(
        "save_invoice",
        route_after_save,
        {"verify_invoice": "verify_invoice", "finish": END},
    )
    graph.add_conditional_edges("verify_invoice", route_after_terminal_check, {"finish": END})

    return graph.compile()


taskpilot_graph = build_agent_graph()
