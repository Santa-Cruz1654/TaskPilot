from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app import db
from app.schemas import InvoiceData, TaskCreate
from app.worker import run_task


BASE_URL = os.getenv("TASKPILOT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


class TaskApprovalDecision(BaseModel):
    """A user's decision for one persisted finance-write proposal."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="TaskPilot",
    description="A reliability-first autonomous invoice task worker running in a local sandbox.",
    version="0.1.0",
    lifespan=lifespan,
)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · TaskPilot</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#5c6670; --line:#dce2e8; --panel:#f5f7fa; --accent:#265d97; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:#fff; color:var(--ink); font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }}
    header {{ background:#132337; color:#fff; padding:14px max(20px, calc((100vw - 1050px)/2)); }}
    header a {{ color:#dbeafe; margin-right:18px; text-decoration:none; }} main {{ max-width:1050px; margin:28px auto; padding:0 20px 50px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }} h2 {{ font-size:19px; margin-top:26px; }} p {{ color:var(--muted); }}
    .panel {{ border:1px solid var(--line); border-radius:12px; padding:18px; margin:16px 0; }}
    label {{ display:block; font-weight:600; margin:12px 0 5px; }} input,select,textarea {{ width:100%; max-width:600px; padding:10px 12px; border:1px solid #b9c3ce; border-radius:7px; font:inherit; }}
    button,.button {{ display:inline-block; border:0; border-radius:7px; padding:10px 15px; background:var(--accent); color:#fff; font:inherit; text-decoration:none; cursor:pointer; margin-top:12px; }}
    table {{ border-collapse:collapse; width:100%; margin-top:12px; }} th,td {{ border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }} th {{ background:var(--panel); }}
    .muted {{ color:var(--muted); }} .pill {{ display:inline-block; background:#edf2f7; border-radius:99px; padding:3px 10px; font-size:13px; }}
    .pill.status-completed,.pill.status-passed {{ background:#e7f6ec; color:#166534; }} .pill.status-failed,.pill.status-error,.pill.status-rejected {{ background:#fcebea; color:#9f2820; }} .pill.status-running,.pill.status-pending,.pill.status-queued,.pill.status-pending_approval {{ background:#fff4d6; color:#854d0e; }}
    .event {{ border-left:3px solid #91a8c0; padding:5px 0 5px 14px; margin:16px 0; }} .event-title {{ font-weight:700; text-transform:capitalize; }} .event-time {{ display:block; margin-top:3px; }}
    .success {{ color:#14743b; }} .error {{ color:#a3362d; }} code {{ background:var(--panel); padding:2px 5px; border-radius:4px; overflow-wrap:anywhere; }}
    pre {{ background:#f5f7fa; padding:12px; white-space:pre-wrap; overflow-wrap:anywhere; border-radius:8px; font-size:13px; }} .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; }}
    .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:16px 0; }} .metric {{ border:1px solid var(--line); border-radius:10px; padding:14px; min-width:0; }} .metric-label {{ color:var(--muted); font-size:13px; margin-bottom:4px; }} .metric-value {{ font-size:18px; font-weight:700; overflow-wrap:anywhere; }}
    .data-table {{ width:100%; border-collapse:collapse; margin:8px 0 16px; }} .data-table th {{ width:34%; font-weight:600; }} .data-table th,.data-table td {{ padding:9px 10px; border-bottom:1px solid var(--line); }}
    details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--accent); font-weight:600; }} .event-data {{ margin-top:8px; }} .check-mark {{ font-weight:700; }}
    button:disabled {{ opacity:.6; cursor:wait; }} .button-secondary {{ background:#667085; }} .button-danger {{ background:#a3362d; }} .approval-panel {{ border-color:#d7a52b; background:#fffaf0; }}
  </style>
</head>
<body><header><strong>TaskPilot</strong> &nbsp; <a href="/">Worker</a><a href="/history">Task history</a><a href="/inbox">Sandbox inbox</a><a href="/finance">Finance app</a><a href="/docs">API docs</a></header>
<main>{body}</main></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    body = """
    <h1>Autonomous AI Task Worker</h1>
    <p>Submit a task in natural language. This first milestone supports invoice processing in a synthetic local environment.</p>
    <div class="panel">
      <form id="task-form">
        <label for="request">Task request</label>
        <textarea id="request" name="request" rows="3" required minlength="5">Find the latest invoice from Acme Supplies, extract the amount and due date, enter it into the finance application, and verify it.</textarea>
        <button type="submit">Run task</button>
      </form>
      <p class="muted">Use one of the sample vendors shown in the <a href="/inbox">inbox</a>. No external website or real credentials are used.</p>
    </div>
    <div id="task-result" class="panel" hidden aria-live="polite"></div>
    <h2>What this prototype demonstrates</h2>
    <div class="grid">
      <div class="panel"><strong>Browser execution</strong><p>Searches the inbox, downloads a PDF, and submits the finance form with Playwright.</p></div>
      <div class="panel"><strong>Independent verification</strong><p>Reads the saved record back and compares the persisted fields against the extracted invoice.</p></div>
      <div class="panel"><strong>Execution history</strong><p>Stores task status and observable events so a run can be inspected.</p></div>
    </div>
    <script>
    const form = document.getElementById('task-form');
    const output = document.getElementById('task-result');
    const submitButton = form.querySelector('button[type="submit"]');
    const escapeHtml = (value) => String(value).replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();

      // Capture the exact request once; use it for submission and as a fallback
      // for displaying the request accepted by the API.
      const submittedRequest = document.getElementById('request').value.trim();
      if (!submittedRequest) return;

      submitButton.disabled = true;
      submitButton.textContent = 'Running…';
      output.hidden = false;
      output.innerHTML = '<strong>Submitting task…</strong>';

      try {
        const response = await fetch('/api/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request: submittedRequest })
        });
        const task = await response.json();
        if (!response.ok) {
          throw new Error(task.detail || 'Could not create task');
        }

        // Build the panel first, then use textContent for the request so user
        // input is displayed as text rather than interpreted as HTML.
        output.innerHTML = `
          <h2>Task <code>${escapeHtml(task.id)}</code></h2>
          <p><strong>Task request:</strong> <span id="task-request"></span></p>
          <p>Status: <span id="status" class="pill">${escapeHtml(task.status)}</span></p>
          <div id="events"></div>
          <div id="result"></div>
        `;
        document.getElementById('task-request').textContent = task.request || submittedRequest;

        const poll = async () => {
          while (true) {
            const taskResponse = await fetch('/api/tasks/' + encodeURIComponent(task.id));
            if (!taskResponse.ok) {
              throw new Error(`Could not retrieve task status (HTTP ${taskResponse.status})`);
            }

            const current = await taskResponse.json();
            document.getElementById('task-request').textContent =
              current.request || task.request || submittedRequest;
            document.getElementById('status').textContent = current.status;

            const events = Array.isArray(current.events) ? current.events : [];
            document.getElementById('events').innerHTML =
              '<h3>Execution timeline</h3>' + events.map((item) => `
                <div class="event">
                  <strong>${escapeHtml(String(item.kind || '').replaceAll('_', ' '))}</strong><br>
                  ${escapeHtml(item.message || '')}<br>
                  <small class="muted">${escapeHtml(item.created_at || '')}</small>
                </div>
              `).join('');

            const reviewLink = current.status === 'pending_approval'
              ? `<p class="approval-panel"><strong>Approval required.</strong> TaskPilot has not written this invoice yet. <a href="/history/${encodeURIComponent(task.id)}">Review the proposal and approve or reject</a>.</p>`
              : '';
            document.getElementById('result').innerHTML = (current.result
              ? '<h3>Result</h3><pre>' + escapeHtml(JSON.stringify(current.result, null, 2)) + '</pre>'
              : '') + reviewLink;

            if (['completed', 'failed', 'needs_input', 'pending_approval', 'rejected'].includes(current.status)) {
              return;
            }

            await new Promise(resolve => window.setTimeout(resolve, 1000));
          }
        };

        await poll();
      } catch (error) {
        output.innerHTML = `<p class="error">${escapeHtml(error instanceof Error ? error.message : String(error))}</p>`;
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'Run task';
      }
    });
    </script>
    """
    return _page("Worker", body)


@app.get("/inbox", response_class=HTMLResponse)
def inbox(q: str = Query(default="", max_length=160)) -> str:
    records = db.list_inbox_invoices(q)
    rows = "".join(
        f"""<tr data-testid="invoice-row" data-invoice-id="{html.escape(row['id'])}"
              data-vendor="{html.escape(row['vendor'])}" data-invoice-number="{html.escape(row['invoice_number'])}"
              data-issue-date="{html.escape(row['issue_date'])}">
          <td data-field="vendor">{html.escape(row['vendor'])}</td>
          <td data-field="invoice_number">{html.escape(row['invoice_number'])}</td>
          <td data-field="issue_date">{html.escape(row['issue_date'])}</td>
          <td>{html.escape(row['currency'])} {html.escape(row['amount'])}</td>
          <td><a href="/invoices/{html.escape(row['id'])}/download">Download {html.escape(row['invoice_number'])} PDF</a></td>
        </tr>"""
        for row in records
    )
    if not rows:
        rows = '<tr><td colspan="5">No invoices matched the search.</td></tr>'
    body = f"""
    <h1>Company inbox</h1><p>All records and documents here are synthetic demo data.</p>
    <div class="panel"><form method="get" action="/inbox"><label for="search">Search invoices</label>
    <input id="search" name="q" value="{html.escape(q)}" placeholder="Vendor or invoice number"><button type="submit">Search</button></form></div>
    <table><thead><tr><th>Vendor</th><th>Invoice number</th><th>Issue date</th><th>Amount</th><th>Document</th></tr></thead><tbody>{rows}</tbody></table>
    """
    return _page("Inbox", body)


@app.get("/invoices/{invoice_id}/download")
def download_invoice(invoice_id: str) -> FileResponse:
    row = db.get_inbox_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    path = db.PDF_DIR / row["pdf_filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Invoice document is missing")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{row['invoice_number']}.pdf",
    )


@app.get("/finance/new", response_class=HTMLResponse)
def new_finance_invoice() -> str:
    body = """
    <h1>Create finance invoice record</h1><p>This simulated application is the target system for the task worker.</p>
    <div class="panel"><form method="post" action="/finance/invoices">
      <label for="vendor">Vendor</label><input id="vendor" name="vendor" required>
      <label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" required>
      <label for="issue_date">Issue date</label><input id="issue_date" name="issue_date" type="date" required>
      <label for="due_date">Due date</label><input id="due_date" name="due_date" type="date" required>
      <label for="amount">Amount</label><input id="amount" name="amount" type="number" min="0.01" step="0.01" required>
      <label for="currency">Currency</label><select id="currency" name="currency"><option value="USD">USD</option><option value="INR">INR</option><option value="EUR">EUR</option><option value="GBP">GBP</option></select>
      <input type="hidden" name="source_invoice_id" value="">
      <button type="submit">Save invoice</button>
    </form></div><p><a href="/finance">View existing finance records</a></p>
    """
    return _page("New finance invoice", body)


@app.post("/finance/invoices", response_class=HTMLResponse)
def create_finance_invoice(
    vendor: Annotated[str, Form(min_length=1, max_length=160)],
    invoice_number: Annotated[str, Form(min_length=1, max_length=80)],
    issue_date: Annotated[str, Form()],
    due_date: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    currency: Annotated[str, Form()],
    source_invoice_id: Annotated[str, Form(min_length=1, max_length=100)],
):
    try:
        invoice = InvoiceData(
            vendor=vendor,
            invoice_number=invoice_number,
            issue_date=issue_date,
            due_date=due_date,
            amount=amount,
            currency=currency,
        )
    except Exception as exc:
        return HTMLResponse(
            _page("Invalid invoice", f"<h1>Invalid invoice</h1><p>{html.escape(str(exc))}</p><a href='/finance/new'>Back</a>"),
            status_code=422,
        )
    try:
        db.insert_finance_invoice(invoice, source_invoice_id)
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            return HTMLResponse(
                _page(
                    "Duplicate invoice",
                    "<h1>Duplicate invoice</h1><p data-testid='duplicate-message'>A record with this vendor and invoice number already exists. No second record was created.</p><a href='/finance'>View finance records</a>",
                ),
                status_code=409,
            )
        raise
    return RedirectResponse(url="/finance?status=saved", status_code=303)


@app.get("/finance", response_class=HTMLResponse)
def finance(status: str = "") -> str:
    records = db.list_finance_invoices()
    rows = "".join(
        f"""<tr data-testid="finance-invoice-row">
          <td data-field="vendor">{html.escape(row['vendor'])}</td>
          <td data-field="invoice_number">{html.escape(row['invoice_number'])}</td>
          <td data-field="issue_date">{html.escape(row['issue_date'])}</td>
          <td data-field="due_date">{html.escape(row['due_date'])}</td>
          <td data-field="amount">{html.escape(row['currency'])} {html.escape(row['amount'])}</td>
          <td>{html.escape(row['created_at'])}</td>
        </tr>"""
        for row in records
    )
    if not rows:
        rows = '<tr><td colspan="6">No finance records have been created yet.</td></tr>'
    saved_notice = '<p class="success" role="status">Invoice saved.</p>' if status == "saved" else ""
    body = f"""
    <h1>Finance application</h1><p>Saved records are persisted in the local SQLite database.</p>{saved_notice}
    <p><a class="button" href="/finance/new">Create invoice record</a></p>
    <table><thead><tr><th>Vendor</th><th>Invoice number</th><th>Issue date</th><th>Due date</th><th>Amount</th><th>Created at</th></tr></thead><tbody>{rows}</tbody></table>
    """
    return _page("Finance", body)


@app.get("/history", response_class=HTMLResponse)
def task_history(limit: int = Query(default=50, ge=1, le=100)) -> str:
    """Render recent tasks from the persistent task store."""
    tasks = db.list_tasks(limit)
    rows = "".join(
        f"""<tr data-testid="task-history-row" data-task-id="{html.escape(str(task.get('id', '')))}">
          <td><a href="/history/{html.escape(str(task.get('id', '')))}"><code>{html.escape(str(task.get('id', '')))}</code></a></td>
          <td>{html.escape(str(task.get('request', '')))}</td>
          <td><span class="pill">{html.escape(str(task.get('status', 'unknown')))}</span></td>
          <td>{html.escape(str(task.get('created_at', '')))}</td>
          <td>{html.escape(str(task.get('updated_at', '')))}</td>
        </tr>"""
        for task in tasks
    )
    if not rows:
        rows = '<tr><td colspan="5">No tasks have been submitted yet.</td></tr>'

    body = f"""
    <h1>Task history</h1>
    <p>Review recent TaskPilot runs, their submitted instructions, current status, and execution details.</p>
    <p><a class="button" href="/">Submit a new task</a></p>
    <table>
      <thead><tr><th>Task ID</th><th>Task request</th><th>Status</th><th>Created</th><th>Last updated</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="muted">Showing up to {limit} most recent tasks. Task records are stored in the local SQLite database.</p>
    """
    return _page("Task history", body)


@app.get("/history/{task_id}", response_class=HTMLResponse)
def task_history_detail(task_id: str) -> HTMLResponse:
    """Render a readable audit view for a persisted task."""
    task = db.get_task(task_id)
    if task is None:
        body = (
            "<h1>Task not found</h1>"
            "<p>The requested task does not exist in the local task store.</p>"
            "<p><a href='/history'>Back to task history</a></p>"
        )
        return HTMLResponse(_page("Task not found", body), status_code=404)

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    def humanize(value: Any) -> str:
        return str(value or "unknown").replace("_", " ").strip().capitalize()

    task_id_value = str(task.get("id", ""))
    task_status = str(task.get("status", "unknown")).lower()
    status_class = task_status if task_status in {
        "completed", "failed", "running", "pending", "queued", "needs_input",
        "pending_approval", "rejected"
    } else "unknown"
    request_value = str(task.get("request", ""))
    created_value = str(task.get("created_at", ""))
    updated_value = str(task.get("updated_at", ""))
    result = task.get("result")
    if not isinstance(result, dict):
        result = {}

    # Build a human-readable invoice summary from the persisted result.
    invoice = result.get("invoice")
    if not isinstance(invoice, dict):
        verification_record = result.get("verification", {})
        invoice = verification_record.get("record", {}) if isinstance(verification_record, dict) else {}
    if not isinstance(invoice, dict):
        invoice = {}

    invoice_fields = [
        ("Vendor", "vendor"),
        ("Invoice number", "invoice_number"),
        ("Issue date", "issue_date"),
        ("Due date", "due_date"),
        ("Amount", "amount"),
        ("Currency", "currency"),
    ]
    invoice_rows = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(invoice.get(key, '—') or '—')}</td></tr>"
        for label, key in invoice_fields
        if invoice.get(key) is not None
    )
    invoice_html = (
        f"<table class='data-table'><tbody>{invoice_rows}</tbody></table>"
        if invoice_rows
        else "<p class='muted'>Invoice details are not available in this task result.</p>"
    )

    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    checks = verification.get("checks", {})
    if not isinstance(checks, dict):
        checks = {}
    check_rows = []
    for key, passed in checks.items():
        label = humanize(key)
        passed_bool = passed is True
        status_text = "Passed" if passed_bool else "Failed"
        status_style = "success" if passed_bool else "error"
        symbol = "✓" if passed_bool else "✕"
        check_rows.append(
            f"<tr><td>{esc(label)}</td><td class='{status_style} check-mark'>"
            f"{symbol} {status_text}</td></tr>"
        )
    check_rows_html = "".join(check_rows)
    total_checks = len(checks)
    passed_checks = sum(value is True for value in checks.values())
    verification_passed = verification.get("passed") is True
    if total_checks:
        verification_html = f"""
        <p><span class='pill status-{'passed' if verification_passed else 'failed'}'>
          {passed_checks}/{total_checks} checks passed
        </span></p>
        <table class='data-table'><thead><tr><th>Verification check</th><th>Outcome</th></tr></thead>
          <tbody>{check_rows_html}</tbody></table>
        """
    else:
        verification_html = "<p class='muted'>No verification checks were recorded for this task.</p>"

    already_existed = result.get("already_existed")
    write_performed = result.get("evidence", {}).get("write_performed") if isinstance(result.get("evidence"), dict) else None
    if write_performed is None:
        # Existing-record outcomes normally explicitly return false. Do not
        # infer a write solely from the task status if the field is absent.
        write_text = "Yes" if write_performed is True else ("No" if write_performed is False else "Not reported")
    else:
        write_text = "Yes" if write_performed else "No"
    if already_existed is True:
        duplicate_text = "Existing record"
    elif already_existed is False:
        duplicate_text = "New record"
    else:
        duplicate_text = "Not reported"

    result_message = result.get("message")
    result_message_html = (
        f"<p class='{'success' if verification_passed else ''}'>{esc(result_message)}</p>"
        if result_message else ""
    )
    raw_result_html = (
        f"<details><summary>View raw result JSON</summary><pre>{esc(json.dumps(result, indent=2, ensure_ascii=False, default=str))}</pre></details>"
        if result else "<p class='muted'>No final result is available yet.</p>"
    )

    # Event data remains available but collapsed, so the main timeline is easy
    # to scan without losing the detailed audit evidence.
    event_cards = []
    for index, event in enumerate(task.get("events", []), start=1):
        kind_raw = str(event.get("kind", "event"))
        kind = esc(humanize(kind_raw))
        message = esc(event.get("message", ""))
        created_at = esc(event.get("created_at", ""))
        data = event.get("data")
        data_html = ""
        if data is not None:
            serialized_data = esc(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            data_html = (
                "<details class='event-data'><summary>View event data</summary>"
                f"<pre>{serialized_data}</pre></details>"
            )
        event_cards.append(
            f"""<div class="event" data-event-kind="{esc(kind_raw)}">
              <span class="pill">Step {index}</span> <span class="event-title">{kind}</span>
              <p>{message}</p>
              <small class="muted event-time">{created_at}</small>
              {data_html}
            </div>"""
        )
    timeline_html = "".join(event_cards) or "<p class='muted'>No events were recorded for this task.</p>"

    # Only show approval controls for a currently pending proposal. The API
    # re-checks the persisted status and action, so the UI is not a trust boundary.
    approval_panel_html = ""
    if task_status == "pending_approval" and isinstance(result.get("proposed_action"), dict):
        proposal = result["proposed_action"]
        proposal_invoice = proposal.get("invoice")
        if isinstance(proposal_invoice, dict) and proposal.get("source_invoice_id"):
            approval_panel_html = f"""
            <div class="panel approval-panel" id="approval-controls" data-task-id="{esc(task_id_value)}">
              <h2>Human approval required</h2>
              <p>TaskPilot has prepared this invoice but has <strong>not submitted the finance write</strong>.</p>
              <p>Review the vendor, invoice number, dates, amount, and currency above. Approval authorizes only this exact proposal.</p>
              <button type="button" id="approve-action">Approve and submit invoice</button>
              <button type="button" id="reject-action" class="button-danger">Reject — do not write</button>
              <p id="approval-feedback" role="status" aria-live="polite"></p>
            </div>
            <script>
            (() => {{
              const panel = document.getElementById('approval-controls');
              if (!panel) return;
              const taskId = panel.dataset.taskId;
              const approveButton = document.getElementById('approve-action');
              const rejectButton = document.getElementById('reject-action');
              const feedback = document.getElementById('approval-feedback');
              const buttons = [approveButton, rejectButton];
              const wait = (ms) => new Promise(resolve => window.setTimeout(resolve, ms));

              async function resolveApproval(decision) {{
                if (buttons.some(button => button.disabled)) return;
                buttons.forEach(button => button.disabled = true);
                feedback.textContent = decision === 'approve'
                  ? 'Submitting approval and resuming TaskPilot…'
                  : 'Recording rejection…';
                try {{
                  const response = await fetch(`/api/tasks/${{encodeURIComponent(taskId)}}/approval`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ decision }})
                  }});
                  const payload = await response.json();
                  if (!response.ok) throw new Error(payload.detail || 'Could not resolve approval');

                  if (decision === 'reject') {{
                    window.location.reload();
                    return;
                  }}

                  feedback.textContent = 'Approved. Waiting for the worker to finish and verify the result…';
                  while (true) {{
                    await wait(1000);
                    const statusResponse = await fetch(`/api/tasks/${{encodeURIComponent(taskId)}}`);
                    if (!statusResponse.ok) throw new Error('Could not retrieve task status');
                    const current = await statusResponse.json();
                    if (!['queued', 'running'].includes(current.status)) {{
                      window.location.reload();
                      return;
                    }}
                  }}
                }} catch (error) {{
                  feedback.textContent = error instanceof Error ? error.message : String(error);
                  buttons.forEach(button => button.disabled = false);
                }}
              }}

              approveButton.addEventListener('click', () => resolveApproval('approve'));
              rejectButton.addEventListener('click', () => resolveApproval('reject'));
            }})();
            </script>
            """

    if task_status == "pending_approval":
        duplicate_text = "Proposed new record"
        write_text = "Not yet — awaiting approval"

    body = f"""
    <p><a href="/history">← Back to task history</a></p>
    <h1>Task details</h1>
    <p>Review the request, workflow timeline, invoice data, and verification evidence for this run.</p>
    <div class="panel">
      <p><strong>Task ID:</strong> <code>{esc(task_id_value)}</code></p>
      <p><strong>Task request:</strong> {esc(request_value)}</p>
      <p><strong>Status:</strong> <span class="pill status-{esc(status_class)}">{esc(humanize(task_status))}</span></p>
      <p><strong>Created:</strong> {esc(created_value)}</p>
      <p><strong>Last updated:</strong> {esc(updated_value)}</p>
    </div>

    <div class="summary-grid">
      <div class="metric"><div class="metric-label">Workflow status</div><div class="metric-value">{esc(humanize(task_status))}</div></div>
      <div class="metric"><div class="metric-label">Invoice record</div><div class="metric-value">{esc(duplicate_text)}</div></div>
      <div class="metric"><div class="metric-label">Finance write performed</div><div class="metric-value">{esc(write_text)}</div></div>
      <div class="metric"><div class="metric-label">Verification</div><div class="metric-value">{f'{passed_checks}/{total_checks}' if total_checks else 'Not reported'}</div></div>
    </div>

    <h2>Invoice details</h2>
    <div class="panel">{invoice_html}</div>

    {approval_panel_html}

    <h2>Verification</h2>
    <div class="panel">{result_message_html}{verification_html}</div>

    <h2>Execution timeline</h2>
    <div class="panel">{timeline_html}</div>

    <h2>Full result data</h2>
    <div class="panel">{raw_result_html}</div>
    <p><a class="button" href="/">Submit another task</a></p>
    """
    return HTMLResponse(_page("Task details", body))


@app.post("/api/tasks", status_code=202)
async def submit_task(payload: TaskCreate, background_tasks: BackgroundTasks):
    task_id = db.create_task(payload.request.strip())
    background_tasks.add_task(run_task, task_id)
    return {"id": task_id, "request": payload.request.strip(), "status": "queued"}


@app.post("/api/tasks/{task_id}/approval", status_code=202)
def resolve_task_approval(
    task_id: str,
    payload: TaskApprovalDecision,
    background_tasks: BackgroundTasks,
):
    """Approve or reject the exact persisted proposal for a pending finance write."""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get("status") != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail="This task is not waiting for approval or has already been resolved.",
        )

    result = task.get("result")
    proposal = result.get("proposed_action") if isinstance(result, dict) else None
    if (
        not isinstance(proposal, dict)
        or proposal.get("type") != "create_finance_invoice"
        or not isinstance(proposal.get("invoice"), dict)
        or not proposal.get("source_invoice_id")
    ):
        raise HTTPException(
            status_code=409,
            detail="The task has no valid pending finance-write proposal.",
        )

    proposed_invoice = proposal["invoice"]
    source_invoice_id = proposal["source_invoice_id"]

    if payload.decision == "approve":
        approval = {
            "decision": "approved",
            "source_invoice_id": source_invoice_id,
            "invoice": proposed_invoice,
            "approved_at": db.utc_now(),
        }
        updated = db.record_task_approval(
            task_id,
            approval,
            new_status="queued",
            result=None,
        )
        if not updated:
            raise HTTPException(
                status_code=409,
                detail="This approval request was already resolved.",
            )
        db.add_event(
            task_id,
            "approval_granted",
            "A human approved the exact proposed invoice fields. TaskPilot is resuming execution.",
            {
                "source_invoice_id": source_invoice_id,
                "invoice_number": proposed_invoice.get("invoice_number"),
                "approved_at": approval["approved_at"],
            },
        )
        background_tasks.add_task(run_task, task_id)
        return {
            "id": task_id,
            "status": "queued",
            "decision": "approved",
            "message": "Approval recorded. TaskPilot will resume and verify the finance write.",
        }

    approval = {
        "decision": "rejected",
        "source_invoice_id": source_invoice_id,
        "invoice": proposed_invoice,
        "rejected_at": db.utc_now(),
    }
    rejected_result = {
        "message": "The proposed finance write was rejected. TaskPilot did not submit the invoice.",
        "invoice": proposed_invoice,
        "source_invoice_id": source_invoice_id,
        "approval": {"decision": "rejected", "rejected_at": approval["rejected_at"]},
        "evidence": {
            "finance_url": f"{BASE_URL}/finance",
            "write_performed": False,
        },
    }
    updated = db.record_task_approval(
        task_id,
        approval,
        new_status="rejected",
        result=rejected_result,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="This approval request was already resolved.",
        )
    db.add_event(
        task_id,
        "approval_rejected",
        "A human rejected the proposed finance write. No write was submitted by TaskPilot.",
        {
            "source_invoice_id": source_invoice_id,
            "invoice_number": proposed_invoice.get("invoice_number"),
            "rejected_at": approval["rejected_at"],
        },
    )
    return {
        "id": task_id,
        "status": "rejected",
        "decision": "rejected",
        "message": "Proposal rejected. TaskPilot did not submit the invoice.",
    }


@app.get("/api/tasks")
def get_tasks(limit: int = Query(default=30, ge=1, le=100)):
    return {"tasks": db.list_tasks(limit)}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/finance/invoices/verify")
def verify_finance_invoice(vendor: str, invoice_number: str):
    """Read-only endpoint intended for a test oracle or independent verification."""
    record = db.get_finance_invoice(vendor, invoice_number)
    if record is None:
        raise HTTPException(status_code=404, detail="Finance record not found")
    return JSONResponse(record)
