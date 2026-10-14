"""P1 internal additions; approved P0 public schemas remain unchanged."""
from typing import Any, Literal

from pydantic import Field

from app.schemas import Claim, Contract, Document, Text, ToolResult
from app.state import RiskState


class Chunk(Contract):
    chunk_id: Text
    document_id: Text
    text: Text
    page: int | None = Field(default=None, ge=1)
    section: Text | None = None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    company_id: Text
    source_group_id: Text


class CorpusEntry(Contract):
    path: Text
    company_id: Text
    title: Text
    url: Text
    published_at: str | None
    source_group_id: Text
    sha256: Text


class ToolCall(Contract):
    name: Text
    arguments: dict[str, Any]


class ResearchTurn(Contract):
    action: Literal['tool', 'finish']
    tool_call: ToolCall | None


class AnalystDraft(Contract):
    claims: list[Claim] = Field(max_length=12)


class ToolEvent(Contract):
    name: Text
    result: ToolResult


class P1State(RiskState):
    documents: list[Document] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
