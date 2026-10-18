import pytest

from app import db


@pytest.fixture(autouse=True)
def isolated_taskpilot_db(tmp_path, monkeypatch):
    """Give each test its own database and invoice directory."""
    data_dir = tmp_path / "data"

    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "PDF_DIR", data_dir / "invoices")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "taskpilot.db")

    db.init_db()

    yield