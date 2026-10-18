from __future__ import annotations

from typing import Any

from app.schemas import InvoiceData


def build_approval_result(
    invoice: InvoiceData,
    source_invoice_id: str,
    base_url: str,
) -> dict[str, Any]:
    """Create the immutable proposal shown to a human before a finance write."""
    invoice_data = invoice.model_dump(mode="json")
    return {
        "message": (
            "A human must approve this proposed finance write before TaskPilot "
            "can create the record. Review the invoice details and choose Approve or Reject."
        ),
        "approval_required": True,
        "pending_approval": True,
        "proposed_action": {
            "type": "create_finance_invoice",
            "source_invoice_id": source_invoice_id,
            "invoice": invoice_data,
        },
        "invoice": invoice_data,
        "source_invoice_id": source_invoice_id,
        "already_existed": False,
        "evidence": {
            "finance_url": f"{base_url.rstrip('/')}/finance",
            "write_performed": False,
        },
    }


def approval_matches(
    approval: dict[str, Any] | None,
    invoice: InvoiceData,
    source_invoice_id: str,
) -> bool:
    """Permit a write only when an explicit approval matches the exact proposal."""
    if not isinstance(approval, dict) or approval.get("decision") != "approved":
        return False

    return (
        approval.get("source_invoice_id") == source_invoice_id
        and approval.get("invoice") == invoice.model_dump(mode="json")
    )
