"""Additive P3 API contracts; P0–P2 report schemas remain unchanged."""
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas import Contract, Gap, ResearchRequest, Text


class RunView(Contract):
    run_id: Text
    status: Literal['queued','running','completed','partial','failed']
    stage: Text
    request: ResearchRequest
    created_at: datetime
    updated_at: datetime
    errors: list[Gap]
    budget: dict[str, Any]
    report_available: bool


class Submission(Contract):
    run_id: Text
    status: Literal['queued'] = 'queued'


class ReportPending(Contract):
    run_id: Text
    status: Literal['queued','running','failed']
    stage: Text
    errors: list[Gap]
    report_available: bool = False


class CompareRequest(Contract):
    run_ids: list[Text] = Field(min_length=2,max_length=2)


class HistoryQuery(Contract):
    company: str | None = None
    as_of_from: date | None = None
    as_of_to: date | None = None
    limit: int = Field(default=20,ge=1,le=100)
    offset: int = Field(default=0,ge=0)
