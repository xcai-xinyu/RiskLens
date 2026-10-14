from pydantic import Field

from app.schemas import (
    Claim,
    Contract,
    Evidence,
    Gap,
    MetricResult,
    ResearchPlan,
    ResearchRequest,
    RiskReport,
    VerificationResult,
)


class Budget(Contract):
    tool_calls: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)


class RiskState(Contract):
    run_id: str
    request: ResearchRequest
    plan: ResearchPlan | None = None
    document_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)
    draft_claims: list[Claim] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    repair_count: int = Field(default=0, ge=0)
    budget: Budget = Field(default_factory=Budget)
    report: RiskReport | None = None
    errors: list[Gap] = Field(default_factory=list)
