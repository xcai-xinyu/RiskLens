"""P1 ModelAdapter capability; no node calls provider SDKs directly."""
import asyncio
import json
import os
from typing import Protocol

from app.adapter import ROOT
from app.p1_contracts import AnalystDraft, ResearchTurn, ToolCall
from app.schemas import ResearchPlan
from app.tools.research import TOOL_SCHEMAS


class ResearchAdapter(Protocol):
    async def make_plan(self, request, company_id): ...
    async def research_turn(self, context): ...
    async def analyze(self, request, evidence, metrics): ...


class DemoResearchAdapter:
    """Versioned response fixtures + ID binding, explicitly simulated."""
    provider = 'fixture'
    model = 'p1-demo-v1'

    def __init__(self):
        self.fixture = json.loads((ROOT/'tests/fixtures/p1_model_v1.json').read_text())
        self.usage = []

    async def make_plan(self, request, company_id):
        return ResearchPlan(company_id=company_id, subquestions=[request.question],
                            search_queries=[request.question], required_metrics=[])

    async def research_turn(self, context):
        if context['tool_events']:
            return ResearchTurn(action='finish', tool_call=None)
        return ResearchTurn(action='tool', tool_call=ToolCall(name='search_documents', arguments={
            'query': context['request']['question'], 'company_id':context['company_id'],
            'as_of_date':context['request']['as_of_date']}))

    async def analyze(self, request, evidence, metrics):
        claims = []
        for template in self.fixture['claims']:
            if template['risk_category'] not in request.risk_categories:
                continue
            source = next((e for e in evidence if template['anchor'] in e.quote), None)
            if source is not None:
                claims.append({k:v for k,v in template.items() if k != 'anchor'} |
                              {'evidence_ids':[source.evidence_id], 'metric_ids':[]})
        return AnalystDraft.model_validate({'claims':claims})


class OpenAIResearchAdapter:
    provider = 'openai'

    def __init__(self, *, max_calls=10):
        from langchain_openai import ChatOpenAI

        from app.providers import chat_configuration
        _, key_name, model_name, missing = chat_configuration(self.provider)
        if missing:
            raise ValueError('provider_configuration_missing:'+','.join(missing))
        if not 0 <= max_calls <= 100:
            raise ValueError('llm_budget_invalid')
        self.model = os.environ[model_name]
        options = {'api_key': os.environ[key_name]}
        if self.provider == 'groq':
            options['base_url'] = 'https://api.groq.com/openai/v1'
        self.client = ChatOpenAI(model=self.model, timeout=30, max_retries=0, **options)
        self.usage = []
        self.failures = []
        self.calls, self.max_calls = 0, max_calls

    async def invoke(self, runnable, messages):
        from app.providers import ProviderFailure, classify_error
        if self.calls >= self.max_calls:
            raise ProviderFailure('llm_budget_exhausted')
        self.calls += 1
        try:
            return await asyncio.wait_for(runnable.ainvoke(messages, config={'callbacks': []}), 30)
        except Exception as exc:  # noqa: BLE001 - only safe classification crosses the boundary
            failure = classify_error(exc)
            self.failures.append(failure.record())
            self.usage.append(None)
            raise failure from None

    def messages(self, role, data):
        return [('system',(ROOT/f'app/prompts/{role}_p1_v1.txt').read_text()),
                ('human',json.dumps(data,ensure_ascii=False))]

    async def structured(self, role, data, schema):
        options = {'strict': True} if self.provider == 'groq' else {}
        runnable = self.client.with_structured_output(schema, method='json_schema',
            include_raw=True, **options)
        result = await self.invoke(runnable, self.messages(role,data))
        raw = result.get('raw')
        self.usage.append(raw.usage_metadata if raw is not None else None)
        if result.get('parsing_error') or result.get('parsed') is None:
            raise ValueError('structured_output_invalid')
        return schema.model_validate(result['parsed'])

    async def make_plan(self, request, company_id):
        return await self.structured('planner',{'request':request.model_dump(mode='json'),
                                              'company_id':company_id},ResearchPlan)

    async def research_turn(self, context):
        schemas = [{'type':'function','function':{'name':name,
            'description':'Bounded research tool; arguments must preserve trusted scope.',
            'parameters':schema.model_json_schema()}} for name,schema in TOOL_SCHEMAS.items()]
        response = await self.invoke(self.client.bind_tools(schemas,parallel_tool_calls=False),
            self.messages('research',context))
        self.usage.append(response.usage_metadata)
        if response.invalid_tool_calls or len(response.tool_calls)>1:
            raise ValueError('tool_output_invalid')
        if not response.tool_calls:
            return ResearchTurn(action='finish',tool_call=None)
        call=response.tool_calls[0]
        return ResearchTurn(action='tool', tool_call=ToolCall(name=call['name'],arguments=call['args']))

    async def analyze(self, request, evidence, metrics):
        return await self.structured('analyst',{'request':request.model_dump(mode='json'),
            'evidence':[e.model_dump(mode='json') for e in evidence],
            'metrics':[m.model_dump(mode='json') for m in metrics]},AnalystDraft)


class GroqResearchAdapter(OpenAIResearchAdapter):
    provider = 'groq'


class APIEmbedding:
    """Explicit configured real-mode embeddings, never silently demo embeddings."""
    def __init__(self):
        from langchain_openai import OpenAIEmbeddings

        from app.providers import embedding_configuration_missing
        embedding_configuration_missing()
        if not os.environ.get('OPENAI_API_KEY') or not os.environ.get('EMBEDDING_MODEL'):
            raise ValueError('embedding_configuration_missing:OPENAI_API_KEY,EMBEDDING_MODEL')
        self.version = 'openai:'+os.environ['EMBEDDING_MODEL']
        self.dimensions = int(os.environ.get('EMBEDDING_DIMENSIONS','1536'))
        if not 1 <= self.dimensions <= 8192:
            raise ValueError('embedding_dimensions_invalid')
        self.client = OpenAIEmbeddings(model=os.environ['EMBEDDING_MODEL'],
            dimensions=self.dimensions, request_timeout=30, max_retries=0, check_embedding_ctx_length=False)
        self.calls = 0

    async def embed(self, texts):
        if self.calls >= 12:
            raise ValueError('embedding_budget_exhausted')
        self.calls += 1
        return await self.client.aembed_documents(texts)
