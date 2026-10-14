import asyncio
import json
from types import SimpleNamespace

import pytest

from app.p1_adapter import APIEmbedding
from app.providers import (
    ProviderFailure,
    chat_configuration,
    classify_error,
    embedding_configuration_missing,
    make_chat_adapter,
)


class Client:
    def __init__(self, **options):
        self.options, self.requests = options, []
        self.exception = None
        self.tool_calls = [{'name':'search_documents', 'args':{'query':'cash', 'company_id':'COIN', 'as_of_date':'2025-02-13'}}]

    def with_structured_output(self, schema, **options):
        self.requests.append(('structured', schema, options))
        payloads = {'ResearchPlan': {'company_id':'COIN','subquestions':[],'search_queries':[],'required_metrics':[]},
                    'AnalystDraft': {'claims':[]}, 'MetricExtraction':{'requests':[]},
                    'SemanticBatch':{'decisions':[]}, 'RepairBatch':{'claims':[]}}
        self.result = {'parsed':schema.model_validate(payloads[schema.__name__]),
                       'raw':SimpleNamespace(usage_metadata={'input_tokens':7,'output_tokens':3,'total_tokens':10}),
                       'parsing_error':None}
        return self

    def bind_tools(self, schemas, **options):
        self.requests.append(('tools', schemas, options))
        self.result = SimpleNamespace(tool_calls=self.tool_calls, invalid_tool_calls=[],
                                     usage_metadata={'input_tokens':9,'output_tokens':2,'total_tokens':11})
        return self

    async def ainvoke(self, messages, **options):
        if self.exception:
            raise self.exception
        return self.result


@pytest.fixture
def configured(monkeypatch):
    import langchain_openai
    monkeypatch.setattr(langchain_openai, 'ChatOpenAI', Client)
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-openai-test')
    monkeypatch.setenv('GROQ_API_KEY', 'fake-groq-test')
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    monkeypatch.delenv('GROQ_MODEL', raising=False)
    return monkeypatch


@pytest.mark.parametrize('provider', ['openai','groq'])
def test_explicit_factory_transport_and_all_p2_contracts(configured, provider):
    from app.schemas import ResearchRequest
    a = make_chat_adapter(provider=provider)
    assert a.provider == provider
    assert a.client.options['max_retries'] == 0 and a.client.options['timeout'] == 30
    assert a.client.options.get('base_url') == ('https://api.groq.com/openai/v1' if provider=='groq' else None)
    request = ResearchRequest(company='Coinbase', question='cash', as_of_date='2025-02-13', risk_categories=['liquidity'], mode='offline_research')
    async def run():
        assert (await a.make_plan(request, 'COIN')).company_id == 'COIN'
        assert (await a.analyze(request, [], [])).claims == []
        assert (await a.extract(request, [], [])).requests == []
        assert (await a.verify([], [], [])).decisions == []
        assert (await a.repair([], [], [], [])).claims == []
    asyncio.run(run())
    assert a.calls == 5 and len(a.usage) == 5
    assert a.usage[0]['total_tokens'] == 10
    for _, _, options in a.client.requests:
        assert options['method']=='json_schema' and options['include_raw']
        assert options.get('strict') == (True if provider=='groq' else None)


def test_groq_tool_normalization_usage_and_single_call(configured):
    a = make_chat_adapter(provider='groq')
    result = asyncio.run(a.research_turn({}))
    assert result.action=='tool' and result.tool_call.name=='search_documents'
    assert a.client.requests[0][2] == {'parallel_tool_calls':False}
    assert a.calls==1 and a.usage[0]['total_tokens']==11
    a.client.tool_calls *= 2
    with pytest.raises(ValueError, match='tool_output_invalid'):
        asyncio.run(a.research_turn({}))


@pytest.mark.parametrize('status,code,expected', [
    (429,None,'provider_rate_limited'), (429,'insufficient_quota','provider_insufficient_balance'),
    (402,None,'provider_insufficient_balance'), (401,None,'provider_authentication'),
    (403,None,'provider_permission'), (404,None,'provider_model_not_found'),
    (400,None,'provider_invalid_request'), (503,None,'provider_unavailable'),
])
def test_safe_provider_classification(status, code, expected):
    error = RuntimeError('SECRET MUST NOT ESCAPE')
    error.status_code, error.code = status, code
    failure = classify_error(error)
    assert failure.code==expected
    assert 'SECRET' not in str(failure) and 'SECRET' not in json.dumps(failure.record())


def test_timeout_and_unknown_errors():
    assert classify_error(TimeoutError('private')).code=='provider_timeout'
    assert classify_error(RuntimeError('private')).code=='provider_error'
    wrapper=RuntimeError('private'); wrapper.__cause__=TimeoutError()
    assert classify_error(wrapper).code=='provider_timeout'


def test_failure_counted_once_no_retry_and_budget_blocks(configured):
    a=make_chat_adapter(provider='groq',max_calls=1)
    error=RuntimeError('private'); error.status_code=429
    a.client.exception=error
    with pytest.raises(ProviderFailure,match='provider_rate_limited'):
        asyncio.run(a.research_turn({}))
    assert a.calls==1 and a.usage==[None]
    assert a.failures==[{'code':'provider_rate_limited','http_status':429,'retryable':True}]
    with pytest.raises(ProviderFailure,match='llm_budget_exhausted'):
        asyncio.run(a.research_turn({}))
    assert a.calls==1 and len(a.failures)==1


def test_async_timeout_is_bounded(configured,monkeypatch):
    import app.p1_adapter
    a=make_chat_adapter(provider='groq')
    async def timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError()
    monkeypatch.setattr(app.p1_adapter.asyncio,'wait_for',timeout)
    with pytest.raises(ProviderFailure,match='provider_timeout'):
        asyncio.run(a.research_turn({}))
    assert a.calls==1 and a.usage==[None]


def test_configuration_is_explicit_and_embedding_independent(configured):
    configured.setenv('LLM_PROVIDER','groq')
    configured.setenv('GROQ_MODEL','separate-groq-model')
    assert make_chat_adapter().model=='separate-groq-model'
    assert make_chat_adapter(provider='openai').model=='test-model'
    configured.delenv('GROQ_API_KEY')
    with pytest.raises(ValueError,match='GROQ_API_KEY'):
        make_chat_adapter()
    configured.delenv('EMBEDDING_MODEL',raising=False)
    assert embedding_configuration_missing()==['EMBEDDING_MODEL']
    configured.setenv('EMBEDDING_PROVIDER','groq')
    with pytest.raises(ValueError,match='unsupported_embedding_provider'):
        APIEmbedding()
    configured.setenv('LLM_PROVIDER','gemini')
    with pytest.raises(ValueError,match='unsupported_provider'):
        chat_configuration()
