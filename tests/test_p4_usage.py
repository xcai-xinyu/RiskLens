"""Simulated transport failures exercise real adapters and runner accounting."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from evaluation.runner import ProviderAdapter, evaluate

KNOWN = {'input_tokens':11, 'output_tokens':7, 'total_tokens':18}


@pytest.mark.parametrize('failure,known,expected', [
    ('parse', True, 'structured_output_invalid'),
    ('validation', True, 'structured_output_invalid'),
    ('parse', False, 'structured_output_invalid'),
    ('timeout', False, 'provider_timeout'),
    ('429', False, 'provider_rate_limited'),
])
@pytest.mark.parametrize('raw_fails', [False, True])
def test_failed_analyst_retains_usage_and_independent_arms(monkeypatch, failure, known, expected, raw_fails):
    import langchain_openai

    instances = []
    class Client:
        def __init__(self, **options):
            self.options = options
            self.index = len(instances)
            instances.append(self)

        def with_structured_output(self, schema, **options):
            self.schema = schema
            return self

        async def ainvoke(self, *args, **kwargs):
            fail = self.index == 0 or raw_fails
            if fail and failure in ('timeout', '429'):
                error = TimeoutError('PRIVATE') if failure == 'timeout' else RuntimeError('PRIVATE')
                if failure == '429':
                    error.status_code = 429
                raise error
            return {'raw':SimpleNamespace(usage_metadata=KNOWN if known else None),
                    'parsed':{'claims':[{'invalid':'PRIVATE'}]} if fail and failure == 'validation' else {'claims':[]},
                    'parsing_error':ValueError('PRIVATE') if fail and failure == 'parse' else None}

    monkeypatch.setattr(langchain_openai, 'ChatOpenAI', Client)
    monkeypatch.setenv('LLM_PROVIDER', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'fake-test-key')
    monkeypatch.setenv('GROQ_MODEL', 'fake-test-model')
    adapters = []

    def factory():
        adapter = ProviderAdapter()
        adapters.append(adapter)
        return adapter

    result = asyncio.run(evaluate('evaluation/datasets/p4-v2', mode='offline_research',
                                 development_case='dev-00', adapter_factory=factory))
    rows = {r['arm']:r for r in result['records']}
    assert set(rows) == {'A','B','C','D','C_REPAIR'}
    assert rows['B']['actual_usage'] == [KNOWN if known else None]
    assert rows['B']['execution_error']['code'] == expected
    assert rows['B']['status'] == 'failed'
    assert rows['A']['status'] == ('failed' if raw_fails else 'completed')
    assert rows['A']['actual_usage'] == [KNOWN if known else None]
    assert rows['A']['execution_error'] == (rows['B']['execution_error'] if raw_fails else None)
    for arm in ('C','D','C_REPAIR'):
        assert rows[arm]['actual_usage'] == [] and rows[arm]['llm_calls'] == 0
        assert rows[arm]['status'] == 'failed'
    shared = result['shared_snapshots'][0]
    assert {rows[a]['shared_snapshot_hash'] for a in ('B','C','D','C_REPAIR')} == {shared['hash']}
    assert shared['snapshot']['draft'] is None
    assert shared['baseline_usage'] == rows['B']['actual_usage']
    assert sum(r['llm_calls'] for r in rows.values()) == sum(a.calls for a in adapters) == 2
    assert all(a.client.options['max_retries'] == 0 and a.client.options['timeout'] == 30 for a in adapters)
    assert all(r['cost'] is None for r in rows.values())
    assert result['aggregate']['B']['token_usage'] == (KNOWN if known else None)
    assert result['aggregate']['C']['token_usage']['total_tokens'] == 0
    assert 'PRIVATE' not in json.dumps(result)


def test_semantic_parse_failure_preserves_incremental_usage_and_pairs(monkeypatch):
    import langchain_openai

    from evaluation.runner import load_dataset

    _, cases, snapshots = load_dataset('evaluation/datasets/p4-v2')
    draft = snapshots[cases[0]['snapshot_id']]['baseline']

    class Client:
        def __init__(self, **options):
            pass

        def with_structured_output(self, schema, **options):
            self.schema = schema
            return self

        async def ainvoke(self, *args, **kwargs):
            semantic = self.schema.__name__ == 'SemanticBatch'
            return {'raw':SimpleNamespace(usage_metadata=KNOWN),
                    'parsed':None if semantic else {'claims':draft},
                    'parsing_error':ValueError('PRIVATE') if semantic else None}

    monkeypatch.setattr(langchain_openai, 'ChatOpenAI', Client)
    monkeypatch.setenv('LLM_PROVIDER', 'groq')
    monkeypatch.setenv('GROQ_API_KEY', 'fake-test-key')
    monkeypatch.setenv('GROQ_MODEL', 'fake-test-model')
    result = asyncio.run(evaluate('evaluation/datasets/p4-v2', mode='offline_research',
                                 development_case='dev-00', adapter_factory=ProviderAdapter))
    rows = {r['arm']:r for r in result['records']}
    assert {rows[a]['shared_snapshot_hash'] for a in ('B','C','D','C_REPAIR')} == {result['shared_snapshots'][0]['hash']}
    assert all(rows[a]['original_draft'] == draft for a in ('B','C','D','C_REPAIR'))
    assert rows['B']['actual_usage'] == [KNOWN]
    for arm in ('C','C_REPAIR'):
        assert rows[arm]['actual_usage'] == [KNOWN]
        assert rows[arm]['llm_calls'] == 1 and rows[arm]['status'] == 'partial'
        assert 'semantic_unavailable' in rows[arm]['output']['gaps']
    assert rows['D']['actual_usage'] == [] and rows['D']['llm_calls'] == 0
    assert sum(r['llm_calls'] for r in rows.values()) == 4
    assert sum(u['total_tokens'] for r in rows.values() for u in r['actual_usage']) == 72


def test_development_budget_shared_across_adapters_without_seventh_call():
    from app.providers import ProviderFailure

    budget = {'calls':0, 'limit':6}
    adapters = []
    for _ in range(2):
        adapter = ProviderAdapter.__new__(ProviderAdapter)
        adapter.call_budget = budget
        adapter.calls, adapter.max_calls = 0, 10
        adapter.failures, adapter.usage = [], []
        adapters.append(adapter)

    class Runnable:
        calls = 0
        async def ainvoke(self, *args, **kwargs):
            self.calls += 1

    runnable = Runnable()
    async def run():
        for index in range(6):
            await adapters[index % 2].invoke(runnable, [])
        with pytest.raises(ProviderFailure, match='llm_budget_exhausted'):
            await adapters[0].invoke(runnable, [])
    asyncio.run(run())
    assert budget['calls'] == runnable.calls == sum(a.calls for a in adapters) == 6
    assert adapters[0].failures == [{'code':'llm_budget_exhausted','http_status':None,'retryable':False}]


def test_cancelled_call_missing_usage_slot_is_unknown_not_zero():
    from evaluation.runner import recorded_usage
    from evaluation.scorers import aggregate

    adapter = SimpleNamespace(calls=2, usage=[KNOWN])
    usage = recorded_usage(adapter)
    assert usage == [KNOWN, None] and adapter.usage == [KNOWN]
    row = {'arm':'B','status':'failed','score':{'metrics':{},'unresolved_claims':[]},
           'latency_seconds':1,'llm_calls':2,'actual_usage':usage}
    assert aggregate([row])['B']['token_usage'] is None
