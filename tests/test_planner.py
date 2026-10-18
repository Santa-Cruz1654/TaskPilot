import asyncio
import json

import pytest

from app import planner
from app.planner import PlannerError


INVOICES = [
    {
        "id": "src-acme-015",
        "vendor": "Acme Supplies",
        "invoice_number": "INV-2026-015",
        "issue_date": "2026-10-11",
    },
    {
        "id": "src-acme-016",
        "vendor": "Acme Supplies",
        "invoice_number": "INV-2026-016",
        "issue_date": "2026-10-14",
    },
]


# ------------------------------------------------------------
# Mock LLM provider
# ------------------------------------------------------------

def install_fake_llm(
    monkeypatch,
    decision,
    api_key_name="TASKPILOT_LLM_API_KEY",
):
    """
    Mock the OpenAI-compatible API.

    No real API calls are made and no API credits are consumed.
    """

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv(
        "TASKPILOT_LLM_API_KEY",
        raising=False,
    )

    monkeypatch.setenv(api_key_name, "test-key")
    monkeypatch.setenv(
        "TASKPILOT_LLM_MODEL",
        "test-model",
    )
    monkeypatch.setenv(
        "TASKPILOT_LLM_BASE_URL",
        "https://example.test/v1/openai",
    )

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(decision),
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(
            self,
            exc_type,
            exc,
            traceback,
        ):
            pass

        async def post(self, url, headers, json):
            assert (
                url
                == "https://example.test/v1/openai/chat/completions"
            )
            assert headers["Authorization"] == "Bearer test-key"
            assert json["model"] == "test-model"
            assert json["temperature"] == 0
            assert json["response_format"] == {
                "type": "json_object"
            }

            return FakeResponse()

    monkeypatch.setattr(
        planner.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )


# ------------------------------------------------------------
# API-key configuration
# ------------------------------------------------------------

def test_planner_requires_api_key(monkeypatch):
    """A non-empty inbox requires a configured provider key."""

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv(
        "TASKPILOT_LLM_API_KEY",
        raising=False,
    )

    with pytest.raises(
        PlannerError,
        match="no API key",
    ):
        asyncio.run(
            planner.plan_invoice_selection(
                "Find the latest invoice from Acme Supplies",
                INVOICES,
            )
        )


@pytest.mark.parametrize(
    "api_key_name",
    [
        "TASKPILOT_LLM_API_KEY",
        "GROQ_API_KEY",
    ],
)
def test_planner_accepts_configured_api_key(
    monkeypatch,
    api_key_name,
):
    """Both supported API-key environment variables work."""

    install_fake_llm(
        monkeypatch,
        {
            "action": "select_invoice",
            "source_invoice_id": "src-acme-016",
            "clarification_question": None,
            "rationale": "Selected the latest Acme invoice.",
        },
        api_key_name=api_key_name,
    )

    decision = asyncio.run(
        planner.plan_invoice_selection(
            "Find the latest invoice from Acme Supplies",
            INVOICES,
        )
    )

    assert decision.action == "select_invoice"
    assert decision.source_invoice_id == "src-acme-016"


# ------------------------------------------------------------
# Valid planner decisions
# ------------------------------------------------------------

def test_planner_accepts_a_valid_observed_invoice(monkeypatch):
    install_fake_llm(
        monkeypatch,
        {
            "action": "select_invoice",
            "source_invoice_id": "src-acme-016",
            "clarification_question": None,
            "rationale": "This is the most recent Acme invoice.",
        },
    )

    decision = asyncio.run(
        planner.plan_invoice_selection(
            "Find the latest invoice from Acme Supplies",
            INVOICES,
        )
    )

    assert decision.action == "select_invoice"
    assert decision.source_invoice_id == "src-acme-016"
    assert decision.clarification_question is None
    assert decision.rationale


def test_planner_can_request_clarification(monkeypatch):
    install_fake_llm(
        monkeypatch,
        {
            "action": "needs_input",
            "source_invoice_id": None,
            "clarification_question": (
                "Which vendor's invoice should I process?"
            ),
            "rationale": "The request does not identify a vendor.",
        },
    )

    decision = asyncio.run(
        planner.plan_invoice_selection(
            "Process an invoice",
            INVOICES,
        )
    )

    assert decision.action == "needs_input"
    assert decision.source_invoice_id is None
    assert decision.clarification_question is not None
    assert "Which vendor" in decision.clarification_question


# ------------------------------------------------------------
# Reject unsafe or invalid planner decisions
# ------------------------------------------------------------

def test_planner_rejects_an_unobserved_invoice_id(monkeypatch):
    install_fake_llm(
        monkeypatch,
        {
            "action": "select_invoice",
            "source_invoice_id": "invented-invoice-id",
            "clarification_question": None,
            "rationale": "Selected an invoice.",
        },
    )

    with pytest.raises(
        PlannerError,
        match="not present in the observed inbox",
    ):
        asyncio.run(
            planner.plan_invoice_selection(
                "Find the latest invoice from Acme Supplies",
                INVOICES,
            )
        )


def test_planner_rejects_inconsistent_decision(monkeypatch):
    install_fake_llm(
        monkeypatch,
        {
            "action": "needs_input",
            "source_invoice_id": "src-acme-016",
            "clarification_question": "Which invoice?",
            "rationale": "The request is ambiguous.",
        },
    )

    with pytest.raises(
        PlannerError,
        match="also returned an invoice ID",
    ):
        asyncio.run(
            planner.plan_invoice_selection(
                "Process an invoice",
                INVOICES,
            )
        )


def test_planner_rejects_invalid_response_schema(monkeypatch):
    install_fake_llm(
        monkeypatch,
        {
            "action": "select_invoice",
            "source_invoice_id": "src-acme-016",
            "unexpected_field": "not allowed",
            "rationale": "Select the latest invoice.",
        },
    )

    with pytest.raises(PlannerError):
        asyncio.run(
            planner.plan_invoice_selection(
                "Find the latest invoice from Acme Supplies",
                INVOICES,
            )
        )


# ------------------------------------------------------------
# Empty inbox handling
# ------------------------------------------------------------

def test_empty_inbox_requests_clarification_without_api_call(
    monkeypatch,
):
    """
    An empty inbox should return needs_input immediately.

    It must not require an API key or make a network request.
    """

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv(
        "TASKPILOT_LLM_API_KEY",
        raising=False,
    )

    class UnexpectedHTTPClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "The planner must not call the API for an empty inbox."
            )

    monkeypatch.setattr(
        planner.httpx,
        "AsyncClient",
        UnexpectedHTTPClient,
    )

    decision = asyncio.run(
        planner.plan_invoice_selection(
            "Find an invoice",
            [],
        )
    )

    assert decision.action == "needs_input"
    assert decision.source_invoice_id is None
    assert decision.clarification_question
    assert "No invoices were found" in (
        decision.clarification_question
    )
    assert decision.rationale