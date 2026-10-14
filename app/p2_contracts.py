"""Additive P2 internal contracts; existing public schemas are preserved."""
from typing import Literal

from pydantic import Field

from app.p1_contracts import P1State
from app.schemas import Claim, Contract, MetricInput, Text, VerificationResult


class MetricRequest(Contract):
    name: Text
    inputs: list[MetricInput] = Field(max_length=2)
    applicable: bool = True


class MetricExtraction(Contract):
    requests: list[MetricRequest] = Field(max_length=5)


class SemanticDecision(Contract):
    claim_id: Text
    status: Literal['supported', 'partially_supported', 'unsupported', 'contradicted', 'unverifiable']
    explanation: Text
    conflicts: list[Text]
    overstatement: bool


class SemanticBatch(Contract):
    decisions: list[SemanticDecision] = Field(max_length=12)


class RepairBatch(Contract):
    claims: list[Claim] = Field(max_length=12)


class Lineage(Contract):
    round: int
    claim: Claim
    verification: VerificationResult
    parent_claim_id: str | None = None


class P2State(P1State):
    lineage: list[Lineage] = Field(default_factory=list)
