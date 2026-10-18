
import asyncio
from decimal import Decimal

import pytest

from app import db, worker
from app.schemas import InvoiceData


def make_invoice(amount="985.75"):
    return InvoiceData(
        vendor="Acme Supplies",
        invoice_number="INV-2026-016",
        issue_date="2026-10-14",
        due_date="2026-11-13",
        amount=Decimal(amount),
        currency="USD",
    )


def make_request():
    return (
        "Find the latest invoice from Acme Supplies, "
        "extract the amount and due date, enter it into "
        "the finance application, and verify it."
    )


# ------------------------------------------------------------
# Vendor selection and request validation
# ------------------------------------------------------------

def test_choose_vendor_from_request():
    rows = db.list_inbox_invoices()

    vendor, invoice_number = worker._choose_vendor_and_invoice(
        make_request(),
        rows,
    )

    assert vendor == "Acme Supplies"
    assert invoice_number is None


def test_choose_explicit_invoice_number():
    rows = db.list_inbox_invoices()

    vendor, invoice_number = worker._choose_vendor_and_invoice(
        "Process invoice OFF-2026-009",
        rows,
    )

    assert vendor == "Northstar Office Co"
    assert invoice_number == "OFF-2026-009"


def test_request_without_invoice_task_is_rejected():
    rows = db.list_inbox_invoices()

    with pytest.raises(worker.NeedsInputError):
        worker._choose_vendor_and_invoice(
            "Open the finance application",
            rows,
        )


def test_unknown_vendor_requests_clarification():
    rows = db.list_inbox_invoices()

    with pytest.raises(
        worker.NeedsInputError,
        match="couldn't identify the vendor",
    ):
        worker._choose_vendor_and_invoice(
            "Process the invoice from Unknown Industries",
            rows,
        )


def test_multiple_vendors_request_clarification():
    rows = db.list_inbox_invoices()

    with pytest.raises(
        worker.NeedsInputError,
        match="multiple vendors",
    ):
        worker._choose_vendor_and_invoice(
            "Process invoices from Acme Supplies "
            "and Northstar Office Co",
            rows,
        )


def test_normalise_ignores_case_and_extra_whitespace():
    assert (
        worker._normalise("  Acme   SUPPLIES ")
        == "acme supplies"
    )


# ------------------------------------------------------------
# Independent verification
# ------------------------------------------------------------

def test_verify_existing_matching_record():
    invoice = make_invoice()

    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    result = worker._verify_record(
        invoice,
        "src-acme-016",
    )

    assert result["passed"] is True
    assert all(result["checks"].values())


def test_verify_fails_when_source_invoice_differs():
    invoice = make_invoice()

    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    result = worker._verify_record(
        invoice,
        "different-source-id",
    )

    assert result["passed"] is False
    assert result["checks"]["record_exists"] is True
    assert result["checks"]["source_document_matches"] is False


def test_verify_fails_when_saved_amount_differs():
    expected = make_invoice()
    incorrect = make_invoice(amount="900.00")

    db.insert_finance_invoice(
        incorrect,
        "src-acme-016",
    )

    result = worker._verify_record(
        expected,
        "src-acme-016",
    )

    assert result["passed"] is False
    assert result["checks"]["amount_matches"] is False


def test_verify_fails_if_record_does_not_exist():
    result = worker._verify_record(
        make_invoice(),
        "src-acme-016",
    )

    assert result["passed"] is False
    assert result["checks"]["record_exists"] is False


# ------------------------------------------------------------
# Lightweight Playwright fakes
# ------------------------------------------------------------

class FakeRow:
    def __init__(self, invoice):
        self.invoice = invoice

    async def get_attribute(self, attribute):
        mapping = {
            "data-invoice-id": "id",
            "data-vendor": "vendor",
            "data-invoice-number": "invoice_number",
            "data-issue-date": "issue_date",
        }

        return self.invoice.get(mapping[attribute])


class FakeRowsLocator:
    def __init__(self, invoices):
        self.invoices = invoices

    async def count(self):
        return len(self.invoices)

    def nth(self, index):
        return FakeRow(self.invoices[index])


class FakeSearchInput:
    async def fill(self, value):
        self.value = value

    async def press(self, key):
        pass


class FakeDownload:
    async def path(self):
        # The invoice parser is stubbed in these tests.
        return "/synthetic-test-invoice.pdf"


class FakeDownloadWaiter:
    def __init__(self, page):
        self.page = page
        self.value = asyncio.get_running_loop().create_future()

    async def __aenter__(self):
        self.page.current_download_waiter = self
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.page.current_download_waiter = None


class FakeClickable:
    def __init__(self, page):
        self.page = page

    async def click(self):
        waiter = self.page.current_download_waiter

        if waiter is not None and not waiter.value.done():
            waiter.value.set_result(FakeDownload())


class FakePage:
    def __init__(self, invoices):
        self.invoices = invoices
        self.current_download_waiter = None

    def set_default_timeout(self, timeout):
        pass

    async def goto(self, url, **kwargs):
        pass

    async def wait_for_load_state(self, state):
        pass

    def locator(self, selector):
        assert selector == "tr[data-testid='invoice-row']"
        return FakeRowsLocator(self.invoices)

    def get_by_label(self, label):
        assert label == "Search invoices"
        return FakeSearchInput()

    def get_by_role(self, role, name=None):
        assert role == "link"
        return FakeClickable(self)

    def expect_download(self):
        return FakeDownloadWaiter(self)


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    async def new_page(self, **kwargs):
        return self.page

    async def close(self):
        pass


class FakeChromium:
    def __init__(self, page):
        self.page = page

    async def launch(self, **kwargs):
        return FakeBrowser(self.page)


class FakePlaywrightContext:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        pass


def install_fake_browser(monkeypatch):
    """
    Replace Playwright with a fake browser containing the
    synthetic inbox. Stub PDF extraction to return Acme's invoice.
    """
    inbox_rows = db.list_inbox_invoices()
    page = FakePage(inbox_rows)

    monkeypatch.setattr(
        worker,
        "async_playwright",
        lambda: FakePlaywrightContext(page),
    )

    monkeypatch.setattr(
        worker,
        "extract_invoice",
        lambda path: make_invoice(),
    )

    return page


# ------------------------------------------------------------
# Integration-level worker tests
# ------------------------------------------------------------

def test_run_task_skips_write_for_matching_existing_invoice(
    monkeypatch,
):
    invoice = make_invoice()

    # Simulate an invoice recorded by an earlier task.
    db.insert_finance_invoice(
        invoice,
        "src-acme-016",
    )

    install_fake_browser(monkeypatch)

    task_id = db.create_task(make_request())

    asyncio.run(worker.run_task(task_id))

    task = db.get_task(task_id)

    assert task is not None
    assert task["status"] == "completed"

    result = task["result"]

    assert result["already_existed"] is True
    assert result["verification"]["passed"] is True
    assert result["evidence"]["write_performed"] is False

    event_kinds = [
        event["kind"]
        for event in task["events"]
    ]

    assert "duplicate_check" in event_kinds
    assert "verification" in event_kinds
    assert "task_completed" in event_kinds
    assert "write_attempted" not in event_kinds

    records = db.list_finance_invoices()

    assert len(records) == 1
    assert records[0]["invoice_number"] == "INV-2026-016"


def test_run_task_stops_for_conflicting_existing_invoice(
    monkeypatch,
):
    conflicting = make_invoice(amount="900.00")

    # Existing record has the same invoice identity but conflicting
    # amount and source document.
    db.insert_finance_invoice(
        conflicting,
        "incorrect-source-id",
    )

    install_fake_browser(monkeypatch)

    task_id = db.create_task(make_request())

    asyncio.run(worker.run_task(task_id))

    task = db.get_task(task_id)

    assert task is not None
    assert task["status"] == "failed"

    result = task["result"]

    assert result["already_existed"] is True
    assert result["verification"]["passed"] is False
    assert result["evidence"]["write_performed"] is False

    checks = result["verification"]["checks"]

    assert checks["amount_matches"] is False
    assert checks["source_document_matches"] is False

    event_kinds = [
        event["kind"]
        for event in task["events"]
    ]

    assert "duplicate_check" in event_kinds
    assert "task_failed" in event_kinds
    assert "write_attempted" not in event_kinds

    # The existing conflicting record must remain unchanged.
    records = db.list_finance_invoices()

    assert len(records) == 1
    assert Decimal(records[0]["amount"]) == Decimal("900.00")
    assert (
        records[0]["source_invoice_id"]
        == "incorrect-source-id"
    )
