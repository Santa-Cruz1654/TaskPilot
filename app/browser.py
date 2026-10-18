from typing import Any


async def read_visible_invoice_rows(page: Any) -> list[dict[str, str]]:
    """Read invoice candidates from the currently displayed inbox page."""
    rows_locator = page.locator("tr[data-testid='invoice-row']")
    rows: list[dict[str, str]] = []

    for index in range(await rows_locator.count()):
        row = rows_locator.nth(index)
        rows.append(
            {
                "id": (await row.get_attribute("data-invoice-id")) or "",
                "vendor": (await row.get_attribute("data-vendor")) or "",
                "invoice_number": (
                    await row.get_attribute("data-invoice-number")
                ) or "",
                "issue_date": (
                    await row.get_attribute("data-issue-date")
                ) or "",
            }
        )

    return rows
