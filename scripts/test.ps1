$ErrorActionPreference = "Stop"
uv sync --all-groups
uv run pytest
uv run ruff check .
