"""Provider-independent P0 contracts; schema version 1."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Category = Literal['liquidity', 'credit', 'market', 'operational', 'regulatory']
Mode = Literal['demo', 'offline_research', 'live']
Level = Literal['LOW', 'MEDIUM', 'HIGH', 'UNKNOWN']
Number = Annotated[Decimal, Field(allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, validate_assignment=True)


class ResearchRequest(Contract):
    company: Text
    question: Text
    as_of_date: date
    risk_categories: list[Category] = Field(min_length=1)
    mode: Mode


class ResearchPlan(Contract):
    company_id: Text
    subquestions: list[Text]
    search_queries: list[Text]
    required_metrics: list[Text]


class Document(Contract):
    document_id: Text
    title: Text
    url: Text | None = None
    local_path: Text | None = None
    source_type: Text
    published_at: date | None
    retrieved_at: datetime
    content_hash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]

    @model_validator(mode='after')
    def location(self):
        if not (self.url or self.local_path):
            raise ValueError('document requires url or local_path')
        return self


class Evidence(Contract):
    evidence_id: Text
    document_id: Text
    chunk_id: Text
    quote: Text
    page: int | None = Field(default=None, ge=1)
    section: Text | None = None
    relevance: float = Field(ge=0, le=1)
    source_group_id: Text

    @model_validator(mode='after')
    def location(self):
        if self.page is None and self.section is None:
            raise ValueError('evidence requires page or section')
        return self


class MetricInput(Contract):
    name: Text
    value: Number | None
    unit: Text
    currency: Text | None
    period: Text
    evidence_id: Text
    extraction_status: Literal['extracted', 'missing', 'invalid']


class MetricResult(Contract):
    metric_id: Text
    name: Text
    value: Number | None
    formula: Text
    inputs: list[MetricInput]
    status: Literal['success', 'missing_input', 'invalid_input', 'not_applicable']
    limitations: list[Text]


class Claim(Contract):
    claim_id: Text
    text: Text
    claim_type: Literal['fact', 'inference']
    risk_category: Category
    evidence_ids: list[Text]
    metric_ids: list[Text]


class VerificationResult(Contract):
    claim_id: Text
    status: Literal['supported', 'partially_supported', 'unsupported', 'contradicted', 'unverifiable']
    checks: dict[str, bool]
    evidence_support_score: float | None = Field(ge=0, le=100)
    explanation: Text
    conflicts: list[Text]


class RiskFactor(Contract):
    category: Category
    level: Level
    verified_claim_ids: list[Text]
    rationale: Text
    missing_data: list[Text]


class Gap(Contract):
    code: Text
    message: Text


class RiskReport(Contract):
    run_id: Text
    company: Text
    as_of_date: date
    status: Literal['complete', 'partial', 'insufficient_evidence']
    overall_risk: Level
    summary: str
    risk_factors: list[RiskFactor]
    claims: list[Claim]
    metrics: list[MetricResult]
    sources: list[Document]
    gaps: list[Gap]
    metadata: dict[str, Any]


class ToolResult(Contract):
    status: Literal['success', 'empty', 'error']
    data: Any = None
    error_code: Text | None = None
    retryable: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def outcome(self):
        if (self.status == 'error') != (self.error_code is not None):
            raise ValueError('only errors require error_code')
        if self.status != 'success' and self.data is not None:
            raise ValueError('empty/error results cannot contain success data')
        if self.status != 'error' and self.retryable:
            raise ValueError('only errors can be retryable')
        return self
