from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskStatus = Literal["queued", "running", "pending_approval", "needs_input", "completed", "failed", "rejected"]


class TaskCreate(BaseModel):
    request: str = Field(min_length=5, max_length=1000)


class InvoiceData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vendor: str = Field(min_length=1, max_length=160)
    invoice_number: str = Field(min_length=1, max_length=80)
    issue_date: str
    due_date: str
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("issue_date", "due_date")
    @classmethod
    def validate_iso_date(cls, value: str) -> str:
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be in YYYY-MM-DD format") from exc
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("currency must be a three-letter code")
        return value

    @model_validator(mode="after")
    def validate_date_order(self) -> "InvoiceData":
        from datetime import date

        if date.fromisoformat(self.due_date) < date.fromisoformat(self.issue_date):
            raise ValueError("due_date cannot be earlier than issue_date")
        return self
