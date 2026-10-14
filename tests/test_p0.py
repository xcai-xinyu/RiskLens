import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapter import DemoAdapter
from app.config import Settings
from app.schemas import (
    Claim,
    Document,
    Evidence,
    MetricInput,
    MetricResult,
    ResearchPlan,
    ResearchRequest,
    RiskFactor,
    ToolResult,
    VerificationResult,
)
from app.service import ResearchService
from app.state import RiskState


def request(**changes):
    return ResearchRequest.model_validate(dict(company='Coinbase', question='Analyze liquidity risk',
        as_of_date='2025-01-01', risk_categories=['liquidity'], mode='demo', **changes))


def run(adapter=None, settings=None):
    return asyncio.run(ResearchService(settings, adapter).run(request()))


@pytest.mark.parametrize('field,value', [('company', ' '), ('question', ''),
    ('as_of_date', 'bad'), ('risk_categories', []), ('risk_categories', ['invented']),
    ('mode', 'fake')])
def test_request_rejects(field, value):
    data = request().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate(data)


def test_contract_roundtrips():
    doc = Document(document_id='d1', title='Fixture', local_path='fixture.txt',
        source_type='text', published_at=None, retrieved_at='2025-01-01T00:00:00Z',
        content_hash='a' * 64)
    evidence = Evidence(evidence_id='e1', document_id='d1', chunk_id='c1', quote='Example',
        section='Example', relevance=1, source_group_id='g1')
    inp = MetricInput(name='cash', value='1.2', unit='million', currency='USD',
        period='2024', evidence_id='e1', extraction_status='extracted')
    metric = MetricResult(metric_id='m1', name='example', value=None, formula='not computed',
        inputs=[inp], status='missing_input', limitations=['P0'])
    claim = Claim(claim_id='c1', text='Example', claim_type='fact', risk_category='liquidity',
        evidence_ids=['e1'], metric_ids=[])
    verification = VerificationResult(claim_id='c1', status='unverifiable', checks={},
        evidence_support_score=None, explanation='No verifier in P0', conflicts=[])
    factor = RiskFactor(category='liquidity', level='UNKNOWN', verified_claim_ids=[],
        rationale='No evidence', missing_data=['evidence'])
    plan = ResearchPlan(company_id='unresolved', subquestions=[], search_queries=[],
        required_metrics=[])
    for obj in [doc, evidence, inp, metric, claim, verification, factor, plan]:
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj
        data = obj.model_dump()
        data['unexpected'] = True
        with pytest.raises(ValidationError):
            type(obj).model_validate(data)
    for obj, field, invalid in [(doc, 'content_hash', 'bad'), (evidence, 'page', 0),
        (inp, 'value', 'NaN'), (metric, 'status', 'made_up'), (claim, 'claim_type', 'opinion'),
        (verification, 'evidence_support_score', 101), (factor, 'level', 'SAFE')]:
        data = obj.model_dump()
        data[field] = invalid
        with pytest.raises(ValidationError):
            type(obj).model_validate(data)


def test_locations_required():
    with pytest.raises(ValidationError):
        Document(document_id='d', title='t', source_type='text', published_at=None,
                 retrieved_at='2025-01-01T00:00:00Z', content_hash='a'*64)
    with pytest.raises(ValidationError):
        Evidence(evidence_id='e', document_id='d', chunk_id='c', quote='q',
                 relevance=1, source_group_id='g')


def test_demo_graph_and_state(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    state = run()
    assert state.plan and state.budget.llm_calls == 1
    assert state.report.metadata['simulated'] is True
    assert 'SIMULATED' in state.report.metadata['label']
    assert state.report.overall_risk == 'UNKNOWN'
    assert state.report.status == 'insufficient_evidence'
    assert not state.report.claims and not state.report.sources and not state.errors
    assert RiskState.model_validate_json(state.model_dump_json()) == state
    data = state.model_dump()
    data['request']['mode'] = 'fake'
    with pytest.raises(ValidationError):
        RiskState.model_validate(data)


@pytest.mark.parametrize('mode', ['live', 'offline_research'])
def test_no_silent_mode_fallback(mode):
    data = request().model_dump()
    data['mode'] = mode
    with pytest.raises(ValueError, match='mode_not_implemented'):
        asyncio.run(ResearchService().run(data))


@pytest.mark.parametrize('config', [{'max_llm_calls': -1}, {'run_timeout_seconds': 0},
    {'max_tool_calls': 101}, {'max_repair_rounds': -1}])
def test_invalid_configuration(config):
    with pytest.raises(ValidationError):
        Settings(**config)


def test_budget_prevents_adapter_call():
    class Forbidden:
        async def plan(self, request):
            pytest.fail('Exhausted budget must not call adapter')
    state = run(Forbidden(), Settings(max_llm_calls=0))
    assert state.budget.llm_calls == 0
    assert state.errors[0].code == 'llm_budget_exhausted'


@pytest.mark.parametrize('failure,code', [('timeout', 'run_timeout'),
    ('parse', 'model_parse_error'), ('error', 'model_error')])
def test_model_failure_terminates_and_redacts(failure, code):
    class Broken:
        async def plan(self, request):
            if failure == 'timeout':
                await asyncio.sleep(1)
            if failure == 'error':
                raise RuntimeError('SECRET_SENTINEL')
            return {'bad': 'SECRET_SENTINEL'}
    state = run(Broken(), Settings(run_timeout_seconds=0.05))
    assert state.errors[0].code == code
    assert state.budget.llm_calls == 1
    assert 'SECRET_SENTINEL' not in state.model_dump_json()


def test_tool_outcomes_distinct():
    results = [ToolResult(status='success', data=[1]), ToolResult(status='empty'),
               ToolResult(status='error', error_code='timeout', retryable=True)]
    assert len({r.status for r in results}) == 3
    with pytest.raises(ValidationError):
        ToolResult(status='error')
    with pytest.raises(ValidationError):
        ToolResult(status='empty', data=0)


def test_fixture_is_explicit_simulation():
    fixture = json.loads(Path('tests/fixtures/demo_v1.json').read_text())
    assert 'SIMULATED' in fixture['simulation_label']
    assert asyncio.run(DemoAdapter().plan(request())).company_id.startswith('unresolved')
