$ErrorActionPreference = "Stop"

uv sync --all-groups
uv run playwright install chromium
uv run uvicorn app.main:app --reload
