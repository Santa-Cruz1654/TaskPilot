from __future__ import annotations
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from playwright.async_api import async_playwright
from pydantic import ValidationError
from app import db
from app.approval import approval_matches, build_approval_result
from app.invoice_parser import InvoiceExtractionError, extract_invoice
from app.agent_graph import taskpilot_graph
from app.browser import read_visible_invoice_rows
from app.planner import PlannerDecision, PlannerError
from app.schemas import InvoiceData


BASE_URL = os.getenv(
    "TASKPILOT_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")


class NeedsInputError(RuntimeError):
    """Raised when a task requires user clarification."""
    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


async def _read_visible_invoice_rows(
    page: Any,
) -> list[dict[str, str]]:
    """Backward-compatible wrapper around the shared browser helper."""
    return await read_visible_invoice_rows(page)


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())

def _money(value: str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))

def _choose_vendor_and_invoice(
    request: str,
    rows: list[dict[str, str]],
) -> tuple[str, str | None]:
    """
    Deterministic selection used when LLM planning is disabled.
    """
    if not any(
        term in request.casefold()
        for term in ("invoice", "bill", "accounts payable")
    ):
        raise NeedsInputError(
            "This prototype currently supports invoice-processing "
            "tasks. Please submit an invoice request."
        )
    request_normalised = _normalise(request)
    explicit_invoice = next(
        (
            row["invoice_number"]
            for row in rows
            if row["invoice_number"].casefold()
            in request_normalised
        ),
        None,
    )
    vendors = sorted(
        {
            row["vendor"]
            for row in rows
            if row["vendor"]
        }
    )
    if explicit_invoice:
        row = next(
            row
            for row in rows
            if row["invoice_number"] == explicit_invoice
        )
        return row["vendor"], explicit_invoice
    matched_vendors = [
        vendor
        for vendor in vendors
        if _normalise(vendor) in request_normalised
    ]
    if len(matched_vendors) == 1:
        return matched_vendors[0], None
    if len(matched_vendors) > 1:
        choices = ", ".join(matched_vendors)
        raise NeedsInputError(
            f"The request mentions multiple vendors ({choices}). "
            "Please specify one vendor."
        )
    choices = ", ".join(vendors)
    raise NeedsInputError(
        "I couldn't identify the vendor in your request. "
        f"Available sandbox vendors: {choices}. "
        "Please name one of them."
    )

def _verify_record(
    expected: InvoiceData,
    source_invoice_id: str,
) -> dict[str, Any]:
    """
    Read the saved finance record and compare it with the extracted
    source invoice. The LLM cannot declare task completion.
    """
    actual = db.get_finance_invoice(
        expected.vendor,
        expected.invoice_number,
    )
    if actual is None:
        return {
            "passed": False,
            "reason": (
                "No saved finance record was found "
                "after submission."
            ),
            "checks": {
                "record_exists": False,
            },
        }
    checks = {
        "record_exists": True,
        "vendor_matches": (
            actual["vendor"].casefold()
            == expected.vendor.casefold()
        ),
        "invoice_number_matches": (
            actual["invoice_number"]
            == expected.invoice_number
        ),
        "issue_date_matches": (
            actual["issue_date"]
            == expected.issue_date
        ),
        "due_date_matches": (
            actual["due_date"]
            == expected.due_date
        ),
        "amount_matches": (
            _money(actual["amount"])
            == _money(expected.amount)
        ),
        "currency_matches": (
            actual["currency"]
            == expected.currency
        ),
        "source_document_matches": (
            actual["source_invoice_id"]
            == source_invoice_id
        ),
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

def _validate_llm_selection(
    request: str,
    decision: Any,
    visible_rows: list[dict[str, str]],
) -> dict[str, str]:
    """
    Check that the model-selected invoice exists in the observed inbox
    and doesn't conflict with an explicitly named vendor or invoice ID.
    """
    planned_row = next(
        (
            row
            for row in visible_rows
            if row["id"] == decision.source_invoice_id
        ),
        None,
    )
    if planned_row is None:
        raise PlannerError(
            "The planner-selected invoice was not present "
            "in the observed inbox."
        )
    request_normalised = _normalise(request)
    # If the user explicitly named an invoice number, that is a
    # hard constraint: the LLM must not select a different invoice.
    explicit_invoice = next(
        (
            row
            for row in visible_rows
            if row["invoice_number"].casefold()
            in request_normalised
        ),
        None,
    )
    if (
        explicit_invoice is not None
        and planned_row["id"] != explicit_invoice["id"]
    ):
        raise PlannerError(
            "The planner selected a different invoice from the "
            "invoice number explicitly specified in the request. "
            "The selection was rejected."
        )
    # Enforce a clearly named vendor where possible.
    known_vendors = sorted(
        {
            row["vendor"]
            for row in visible_rows
            if row["vendor"]
        }
    )
    mentioned_vendors = [
        vendor
        for vendor in known_vendors
        if _normalise(vendor) in request_normalised
    ]
    if len(mentioned_vendors) > 1:
        raise NeedsInputError(
            "The request mentions multiple vendors: "
            + ", ".join(mentioned_vendors)
            + ". Please specify one vendor."
        )
    if (
        len(mentioned_vendors) == 1
        and _normalise(planned_row["vendor"])
        != _normalise(mentioned_vendors[0])
    ):
        raise PlannerError(
            "The planner selected an invoice from a different "
            "vendor than the vendor explicitly named in the request. "
            "The selection was rejected."
        )
    return planned_row

async def run_task(task_id: str) -> None:
    task = db.get_task(task_id)
    if task is None:
        return
    # A rejected proposal is terminal. Do not allow a direct/background call
    # to restart it and accidentally perform the rejected action.
    if task.get("status") == "rejected":
        return

    request = task["request"]
    approval = task.get("approval")
    db.set_task_status(task_id, "running")
    db.add_event(
        task_id,
        "worker_started",
        "Worker started; opening the local inbox.",
    )
    browser = None
    try:
        async with async_playwright() as playwright:
            chromium_executable = os.getenv(
                "TASKPILOT_CHROMIUM_EXECUTABLE"
            )
            launch_options: dict[str, Any] = {
                "headless": True,
            }
            if chromium_executable:
                launch_options["executable_path"] = (
                    chromium_executable
                )
            browser = await playwright.chromium.launch(
                **launch_options
            )
            page = await browser.new_page(
                accept_downloads=True
            )
            page.set_default_timeout(7_000)
            planner_mode = os.getenv(
                "TASKPILOT_PLANNER_MODE",
                "deterministic",
            ).strip().casefold()
            planned_source_invoice_id: str | None = None

            if planner_mode == "llm":
                # The worker owns the browser lifecycle. The graph receives
                # the live page through runtime context and owns inbox
                # navigation/observation plus invoice planning.
                graph_result = await taskpilot_graph.ainvoke(
                    {
                        "task_id": task_id,
                        "task_request": request,
                        "status": "pending",
                        "current_step": "validation",
                        "events": [],
                        "persist_events": True,
                        "approval": approval,
                    },
                    context={"page": page, "base_url": BASE_URL},
                )

                # The LLM-mode graph owns the complete workflow. Persist its
                # terminal state and return before entering the legacy worker
                # execution path; otherwise a successful graph write could be
                # followed by a second, misleading manual-path execution.
                graph_status = graph_result.get("status")
                if graph_status in {
                    "pending_approval", "completed", "failed", "needs_input"
                }:
                    result = graph_result.get("result")
                    if not isinstance(result, dict):
                        result = {
                            "message": graph_result.get("error")
                            or f"LangGraph ended with status: {graph_status}"
                        }
                    db.set_task_status(task_id, graph_status, result)
                    return

                # The compiled graph must reach an explicit terminal state.
                # Never fall back to a second, legacy execution path because
                # that could bypass approval or repeat a write.
                result = {
                    "message": (
                        "LangGraph returned without a supported terminal status. "
                        "TaskPilot stopped without attempting a second execution path."
                    ),
                    "error_type": "UnexpectedGraphStatus",
                    "graph_status": graph_status,
                    "evidence": {"write_performed": False},
                }
                db.set_task_status(task_id, "failed", result)
                db.add_event(
                    task_id,
                    "task_failed",
                    result["message"],
                    result,
                )
                return

            elif planner_mode == "deterministic":
                # Keep the deterministic path working as before. It performs
                # observation in the worker until it is migrated to the graph.
                await page.goto(
                    f"{BASE_URL}/inbox",
                    wait_until="domcontentloaded",
                )
                visible_rows = await read_visible_invoice_rows(page)
                db.add_event(
                    task_id,
                    "observation",
                    "Inspected the inbox and collected visible invoice candidates.",
                    {"visible_invoice_count": len(visible_rows)},
                )

                vendor, explicit_invoice_number = _choose_vendor_and_invoice(
                    request,
                    visible_rows,
                )
            else:
                raise PlannerError(
                    "Unsupported TASKPILOT_PLANNER_MODE. "
                    "Use 'deterministic' or 'llm'."
                )

            # --------------------------------------------------
            # Step 2: Search and resolve the selected invoice.
            # --------------------------------------------------
            search = page.get_by_label("Search invoices")
            await search.fill(vendor)
            await search.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
            vendor_rows = [
                row
                for row in await read_visible_invoice_rows(page)
                if _normalise(row["vendor"])
                == _normalise(vendor)
            ]
            if explicit_invoice_number:
                candidates = [
                    row
                    for row in vendor_rows
                    if row["invoice_number"]
                    == explicit_invoice_number
                ]
            else:
                candidates = vendor_rows
            if planned_source_invoice_id is not None:
                candidates = [
                    row
                    for row in candidates
                    if row["id"] == planned_source_invoice_id
                ]
            if not candidates:
                raise NeedsInputError(
                    f"No invoice for {vendor} matched the request "
                    "and the observed search results. "
                    "No finance record was created."
                )
            if planned_source_invoice_id is not None:
                selected = candidates[0]
            else:
                selected = max(
                    candidates,
                    key=lambda row: row["issue_date"],
                )
            source_invoice_id = selected["id"]
            db.add_event(
                task_id,
                "plan_action",
                (
                    "Selected the invoice after checking the "
                    "observed search results."
                    if planner_mode == "llm"
                    else (
                        "Selected the latest matching invoice "
                        "from the observed search results."
                    )
                ),
                {
                    "source_invoice_id": source_invoice_id,
                    "vendor": selected["vendor"],
                    "invoice_number": selected["invoice_number"],
                    "issue_date": selected["issue_date"],
                    "planner_mode": planner_mode,
                },
            )
            # --------------------------------------------------
            # Step 3: Download and extract the PDF.
            # --------------------------------------------------
            async with page.expect_download() as download_info:
                await page.get_by_role(
                    "link",
                    name=(
                        f"Download {selected['invoice_number']} PDF"
                    ),
                ).click()
            download = await download_info.value
            pdf_path = await download.path()
            if not pdf_path:
                raise InvoiceExtractionError(
                    "The browser did not provide a downloaded "
                    "invoice file."
                )
            invoice = extract_invoice(Path(pdf_path))
            # Ensure the downloaded document matches the selected row.
            if (
                _normalise(invoice.vendor)
                != _normalise(selected["vendor"])
                or invoice.invoice_number
                != selected["invoice_number"]
                or invoice.issue_date
                != selected["issue_date"]
            ):
                raise InvoiceExtractionError(
                    "The downloaded document does not match "
                    "the invoice row selected in the inbox."
                )
            db.add_event(
                task_id,
                "document_extracted",
                "Downloaded the PDF and extracted validated invoice fields.",
                {
                    "vendor": invoice.vendor,
                    "invoice_number": invoice.invoice_number,
                    "issue_date": invoice.issue_date,
                    "due_date": invoice.due_date,
                    "amount": str(
                        invoice.amount.quantize(Decimal("0.01"))
                    ),
                    "currency": invoice.currency,
                },
            )
            # --------------------------------------------------
            # Step 4: Prevent duplicate or conflicting writes.
            # --------------------------------------------------
            existing_record = db.get_finance_invoice(
                invoice.vendor,
                invoice.invoice_number,
            )
            if existing_record is not None:
                db.add_event(
                    task_id,
                    "duplicate_check",
                    (
                        "An existing finance record was found. "
                        "Verifying it before deciding whether to write."
                    ),
                    {
                        "invoice_number": invoice.invoice_number,
                        "existing_source_invoice_id": (
                            existing_record["source_invoice_id"]
                        ),
                        "requested_source_invoice_id": (
                            source_invoice_id
                        ),
                    },
                )
                verification = _verify_record(
                    invoice,
                    source_invoice_id,
                )
                db.add_event(
                    task_id,
                    "verification",
                    (
                        "Existing finance record matches the source invoice."
                        if verification["passed"]
                        else (
                            "Existing finance record does not "
                            "match the source invoice."
                        )
                    ),
                    verification,
                )
                if verification["passed"]:
                    result = {
                        "message": (
                            "Invoice was already recorded and "
                            "independently verified. No duplicate "
                            "was created."
                        ),
                        "invoice": invoice.model_dump(mode="json"),
                        "source_invoice_id": source_invoice_id,
                        "already_existed": True,
                        "verification": verification,
                        "evidence": {
                            "finance_url": f"{BASE_URL}/finance",
                            "verification_checks_passed": sum(
                                verification["checks"].values()
                            ),
                            "verification_checks_total": len(
                                verification["checks"]
                            ),
                            "write_performed": False,
                        },
                    }
                    db.set_task_status(
                        task_id,
                        "completed",
                        result,
                    )
                    db.add_event(
                        task_id,
                        "task_completed",
                        result["message"],
                        result["evidence"],
                    )
                    return
                # Never overwrite a conflicting record or report success.
                result = {
                    "message": (
                        "An existing finance record was found, "
                        "but it does not match the source invoice. "
                        "No duplicate was created. Manual review "
                        "is required."
                    ),
                    "invoice": invoice.model_dump(mode="json"),
                    "source_invoice_id": source_invoice_id,
                    "already_existed": True,
                    "verification": verification,
                    "evidence": {
                        "finance_url": f"{BASE_URL}/finance",
                        "write_performed": False,
                    },
                }
                db.set_task_status(
                    task_id,
                    "failed",
                    result,
                )
                db.add_event(
                    task_id,
                    "task_failed",
                    result["message"],
                    result,
                )
                return

            # --------------------------------------------------
            # Step 5: Require human approval before a new finance write.
            # Existing matching records above are read-only and do not require
            # approval because the workflow will not create or change anything.
            # --------------------------------------------------
            if not approval:
                proposal = build_approval_result(
                    invoice,
                    source_invoice_id,
                    BASE_URL,
                )
                db.set_task_status(task_id, "pending_approval", proposal)
                db.add_event(
                    task_id,
                    "approval_requested",
                    (
                        "A human must approve the proposed finance write. "
                        "No finance record has been submitted."
                    ),
                    proposal["proposed_action"],
                )
                return

            if not approval_matches(approval, invoice, source_invoice_id):
                result = {
                    "message": (
                        "The stored approval does not match the exact invoice proposal "
                        "currently observed. The finance write was blocked."
                    ),
                    "error_type": "ApprovalMismatch",
                    "invoice": invoice.model_dump(mode="json"),
                    "source_invoice_id": source_invoice_id,
                    "evidence": {
                        "finance_url": f"{BASE_URL}/finance",
                        "write_performed": False,
                    },
                }
                db.set_task_status(task_id, "failed", result)
                db.add_event(
                    task_id,
                    "approval_validation_failed",
                    result["message"],
                    result,
                )
                return

            db.add_event(
                task_id,
                "approval_validated",
                "Stored human approval matches the exact extracted invoice; proceeding with the finance write.",
                {
                    "source_invoice_id": source_invoice_id,
                    "invoice_number": invoice.invoice_number,
                },
            )

            # --------------------------------------------------
            # Step 6: Submit the finance form using Playwright.
            # --------------------------------------------------
            await page.goto(
                f"{BASE_URL}/finance/new",
                wait_until="domcontentloaded",
            )
            await page.get_by_label("Vendor").fill(invoice.vendor)
            await page.get_by_label("Invoice number").fill(
                invoice.invoice_number
            )
            await page.get_by_label("Issue date").fill(
                invoice.issue_date
            )
            await page.get_by_label("Due date").fill(
                invoice.due_date
            )
            await page.get_by_label("Amount").fill(
                f"{invoice.amount:.2f}"
            )
            await page.get_by_label("Currency").select_option(
                invoice.currency
            )
            # Hidden metadata field: only use the inbox-observed ID.
            await page.locator(
                "input[name='source_invoice_id']"
            ).evaluate(
                "(element, value) => { element.value = value; }",
                source_invoice_id,
            )
            await page.get_by_role(
                "button",
                name="Save invoice",
            ).click()
            db.add_event(
                task_id,
                "write_attempted",
                (
                    "Submitted the invoice through the "
                    "finance application's web form."
                ),
                {
                    "invoice_number": invoice.invoice_number,
                    "source_invoice_id": source_invoice_id,
                },
            )
            # --------------------------------------------------
            # Step 6: Independently verify the saved record.
            # --------------------------------------------------
            verification = _verify_record(
                invoice,
                source_invoice_id,
            )
            db.add_event(
                task_id,
                "verification",
                (
                    "Independent read-back verification passed."
                    if verification["passed"]
                    else (
                        "Independent read-back verification failed."
                    )
                ),
                verification,
            )
            if not verification["passed"]:
                result = {
                    "message": (
                        "The worker could not verify that the "
                        "finance record matches the invoice."
                    ),
                    "invoice": invoice.model_dump(mode="json"),
                    "source_invoice_id": source_invoice_id,
                    "already_existed": False,
                    "verification": verification,
                    "evidence": {
                        "finance_url": f"{BASE_URL}/finance",
                        "write_performed": True,
                    },
                }
                db.set_task_status(
                    task_id,
                    "failed",
                    result,
                )
                db.add_event(
                    task_id,
                    "task_failed",
                    result["message"],
                )
                return
            # --------------------------------------------------
            # Step 7: Report success only after verification.
            # --------------------------------------------------
            result = {
                "message": (
                    "Invoice recorded and independently verified."
                ),
                "invoice": invoice.model_dump(mode="json"),
                "source_invoice_id": source_invoice_id,
                "already_existed": False,
                "verification": verification,
                "evidence": {
                    "inbox_url": (
                        f"{BASE_URL}/inbox"
                        f"?q={vendor.replace(' ', '+')}"
                    ),
                    "finance_url": f"{BASE_URL}/finance",
                    "verification_checks_passed": sum(
                        verification["checks"].values()
                    ),
                    "verification_checks_total": len(
                        verification["checks"]
                    ),
                    "write_performed": True,
                },
            }
            db.set_task_status(
                task_id,
                "completed",
                result,
            )
            db.add_event(
                task_id,
                "task_completed",
                result["message"],
                result["evidence"],
            )
    except NeedsInputError as exc:
        result = {
            "message": str(exc),
            "clarification_question": str(exc),
        }
        db.set_task_status(
            task_id,
            "needs_input",
            result,
        )
        db.add_event(
            task_id,
            "needs_input",
            str(exc),
        )
    except PlannerError as exc:
        result = {
            "message": str(exc),
            "error_type": "PlannerError",
        }
        db.set_task_status(
            task_id,
            "failed",
            result,
        )
        db.add_event(
            task_id,
            "planner_failed",
            str(exc),
            result,
        )
    except (
        InvoiceExtractionError,
        ValidationError,
    ) as exc:
        result = {
            "message": str(exc),
            "error_type": type(exc).__name__,
        }
        db.set_task_status(
            task_id,
            "failed",
            result,
        )
        db.add_event(
            task_id,
            "task_failed",
            "Invoice extraction or validation failed.",
            result,
        )
    except Exception as exc:
        result = {
            "message": (
                f"Execution failed: {type(exc).__name__}: {exc}"
            ),
            "error_type": type(exc).__name__,
        }
        db.set_task_status(
            task_id,
            "failed",
            result,
        )
        db.add_event(
            task_id,
            "task_failed",
            result["message"],
            result,
        )
    finally:
        if browser is not None:
            await browser.close()
