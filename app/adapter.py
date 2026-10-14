"""All model calls cross this boundary. No .env loading or secret logging."""
import json
import os
from pathlib import Path
from typing import Protocol

from app.schemas import ResearchPlan, ResearchRequest

ROOT = Path(__file__).resolve().parent.parent


class ModelAdapter(Protocol):
    async def plan(self, request: ResearchRequest) -> ResearchPlan: ...


class DemoAdapter:
    async def plan(self, request):
        fixture = json.loads((ROOT / 'tests/fixtures/demo_v1.json').read_text())
        return ResearchPlan.model_validate(fixture['plan'])


class OpenAIAdapter:
    """Isolated opt-in structured-output smoke, not a live research workflow."""
    async def plan(self, request):
        from langchain_openai import ChatOpenAI
        if not os.environ.get('OPENAI_API_KEY') or not os.environ.get('LLM_MODEL'):
            raise ValueError('provider_configuration_missing')
        if os.environ.get('LLM_PROVIDER', 'openai') != 'openai':
            raise ValueError('unsupported_provider')
        model = ChatOpenAI(model=os.environ['LLM_MODEL'], timeout=30, max_retries=0)
        structured = model.with_structured_output(ResearchPlan, method='json_schema')
        result = await structured.ainvoke([
            ('system', (ROOT / 'app/prompts/smoke_v1.txt').read_text()),
            ('human', request.model_dump_json()),
        ], config={'callbacks': []})
        return ResearchPlan.model_validate(result)
