"""P2 financial correctness and adversarial adjudication; no live-provider claims."""
import asyncio
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.p1_adapter import DemoResearchAdapter
from app.p1_contracts import AnalystDraft, CorpusEntry
from app.p2_adapter import DemoP2Adapter
from app.p2_contracts import MetricExtraction, P2State, RepairBatch, SemanticBatch, SemanticDecision
from app.p2_workflow import P2Workflow
from app.rag.ingestion import ingest_bytes
from app.reporting import render_json, render_markdown
from app.schemas import Claim, Evidence, MetricInput, ResearchRequest
from app.service import ResearchService
from app.storage.repository import Repository
from app.tools.metrics import FORMULAS, calculate_metric, parse_number
from app.verification import adjudicate, hard_checks

ROOT=Path(__file__).resolve().parent.parent
ADVERSARIAL=json.loads((ROOT/'tests/fixtures/regression/p2_adversarial_v2.json').read_text())


def request(**changes):
    return ResearchRequest.model_validate({'company': 'Coinbase','question': 'capital liquidity USD resources SEC appeal discovery',
        'as_of_date': '2025-02-13','risk_categories': ['liquidity','regulatory'],'mode': 'demo'} | changes)


def inputs(name='current_ratio', **changes):
    first,second,_=FORMULAS[name]
    period='FY:2024'
    data=[{'name': first,'value': '200','unit': 'million','currency': 'USD','period': period,
               'evidence_id': 'ev-test','extraction_status': 'extracted'},
          {'name': second,'value': '100','unit': 'million','currency': 'USD','period': 'FY:2023' if name=='operating_cash_flow_change' else period,
               'evidence_id': 'ev-test','extraction_status': 'extracted'}]
    data[1].update(changes)
    return [MetricInput.model_validate(i) for i in data]


@pytest.mark.parametrize('name,expected',[(n,'1' if n=='operating_cash_flow_change' else '2') for n in FORMULAS])
def test_registered_formulas(name,expected):
    m=calculate_metric(name,inputs(name))
    assert m.status=='success' and m.value==Decimal(expected)
    assert m.inputs[0].evidence_id=='ev-test' and m.formula==FORMULAS[name][2]
    assert calculate_metric(name,m.inputs)==m


@pytest.mark.parametrize('unit,value',[('units','100000000'),('thousand','100000'),('million','100'),('billion','0.1')])
def test_units(unit,value):
    m=calculate_metric('current_ratio',inputs(unit=unit,value=value))
    assert m.value==Decimal(2) and m.inputs[1].unit==unit


@pytest.mark.parametrize('name',list(FORMULAS))
def test_zero_denominator(name):
    m=calculate_metric(name,inputs(name,value='0'))
    assert m.value is None and m.status in ('invalid_input','not_applicable')


def test_negative_equity_and_ocf():
    m=calculate_metric('debt_to_equity',inputs('debt_to_equity',value='-20'))
    assert m.status=='not_applicable' and m.limitations==['nonpositive_equity']
    m=calculate_metric('operating_cash_flow_change',inputs('operating_cash_flow_change',value='-100'))
    assert m.value==Decimal(3) and m.limitations==['negative_previous_OCF','OCF_sign_reversal']
    assert calculate_metric('current_ratio',inputs(),applicable=False).status=='not_applicable'


@pytest.mark.parametrize('changes,code',[({'period':'Q4:2024'},'period_mismatch'),
    ({'currency':'EUR'},'currency_mismatch'),({'unit':'percent'},'unit_mismatch'),
    ({'value':None,'extraction_status':'missing'},'missing_input')])
def test_input_validation(changes,code):
    m=calculate_metric('current_ratio',inputs(**changes))
    assert m.value is None and code in m.limitations


@pytest.mark.parametrize('period',['FY:2022','Q4:2023','TTM:2023','2023'])
def test_ocf_period_mismatch(period):
    assert calculate_metric('operating_cash_flow_change',inputs('operating_cash_flow_change',period=period)).limitations==['period_mismatch']


def test_missing_and_unregistered():
    assert calculate_metric('cash_ratio',[]).status=='missing_input'
    with pytest.raises(ValueError,match='unregistered_formula'):
        calculate_metric('__import__("os").system("echo unsafe")',[])
    assert parse_number('(1,234.50)')==Decimal('-1234.50')
    assert parse_number('12.5%')==Decimal('0.125')
    with pytest.raises(ValueError):
        parse_number('NaN')


@pytest.fixture
def store(tmp_path):
    repo=Repository(tmp_path)
    texts=[ADVERSARIAL['source'],ADVERSARIAL['conflicting_source']]
    ev=[]
    for n,text in enumerate(texts):
        raw=text.encode()
        entry=CorpusEntry(path=f'{n}.txt',company_id='COIN',title=f'Synthetic source {n}',
            url=f'https://example.com/{n}',published_at='2025-01-01',source_group_id=f'group-{n}',sha256=hashlib.sha256(raw).hexdigest())
        assert ingest_bytes(repo,raw,'.txt',entry).status=='success'
    for c in repo.chunks():
        ev.append(Evidence(evidence_id='ev-'+c.chunk_id.removeprefix('chunk-'),document_id=c.document_id,
            chunk_id=c.chunk_id,quote=c.text,page=c.page,section=c.section,relevance=1,source_group_id=c.source_group_id))
    yield repo,ev
    repo.close()


def claim(evidence,**changes):
    return Claim.model_validate({'claim_id': 'c1','text': 'Cash is 20 million USD FY:2024.',
        'claim_type': 'fact','risk_category': 'liquidity','evidence_ids': [evidence.evidence_id],'metric_ids': []} | changes)


def decision(c,status='supported',**changes):
    return SemanticDecision.model_validate({'claim_id': c.claim_id,'status': status,
        'explanation': 'Synthetic annotated semantic fixture decision.','conflicts': [],'overstatement': False} | changes)


@pytest.mark.parametrize('status',['supported','partially_supported','unsupported','contradicted','unverifiable'])
def test_five_statuses(store,status):
    repo,ev=store;c=claim(ev[0])
    checks,reasons=hard_checks(c,ev,[],repo,date(2025,2,13))
    assert all(checks.values())
    v=adjudicate(c,checks,reasons,decision(c,status))
    assert v.status==status
    assert (v.evidence_support_score is not None)==(status in ('supported','partially_supported'))


@pytest.mark.parametrize('damage',['id','quote','page','date','unknown_date','hash'])
def test_hard_failure_overrides_semantics(store,damage):
    repo,ev=store;c=claim(ev[0])
    cutoff=date(2025,2,13)
    if damage=='id':
        c.evidence_ids=['fabricated']
    elif damage=='quote':
        ev[0]=ev[0].model_copy(update={'quote':'invented quote'})
    elif damage=='page':
        ev[0]=ev[0].model_copy(update={'page':999})
    elif damage=='date':
        cutoff=date(2024,12,31)
    elif damage=='unknown_date':
        doc=repo.document(ev[0].document_id);doc.published_at=None
        repo.connection.execute('UPDATE documents SET payload=? WHERE id=?',(doc.model_dump_json(),doc.document_id))
    else:
        doc=repo.document(ev[0].document_id)
        (repo.directory/(doc.content_hash+'.txt')).write_text('corrupted')
    checks,reasons=hard_checks(c,ev,[],repo,cutoff)
    v=adjudicate(c,checks,reasons,decision(c))
    assert v.status!='supported' and v.evidence_support_score is None


def test_recalculation_and_input_source(store):
    repo,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    items=[i.model_copy(update={'evidence_id':source.evidence_id}) for i in inputs()]
    m=calculate_metric('current_ratio',items)
    c=claim(source).model_copy(update={'metric_ids':[m.metric_id]})
    checks,_=hard_checks(c,ev,[m],repo,date(2025,2,13))
    assert all(checks.values())
    tampered=m.model_copy(update={'value':Decimal(900)})
    assert not hard_checks(c,ev,[tampered],repo,date(2025,2,13))[0]['recalculable']
    fake=calculate_metric('current_ratio',[i.model_copy(update={'value':Decimal(999)}) for i in items])
    c.metric_ids=[fake.metric_id]
    assert not hard_checks(c,ev,[fake],repo,date(2025,2,13))[0]['metric_inputs']
    c.metric_ids=['fabricated-metric']
    assert not hard_checks(c,ev,[],repo,date(2025,2,13))[0]['citation_ids']


def test_conflicts_and_overstatement(store):
    repo,ev=store;c=claim(ev[0]);checks,reasons=hard_checks(c,ev,[],repo,date(2025,2,13))
    v=adjudicate(c,checks,reasons,decision(c,conflicts=['Source 1: 20; source 2: 10, same period.']))
    assert v.status=='unverifiable' and v.conflicts and v.evidence_support_score is None
    assert adjudicate(c,checks,reasons,decision(c,overstatement=True)).status=='partially_supported'


class FixtureAdapter(DemoResearchAdapter):
    """Scripted semantic answers exercise routing; they are not real-model quality evidence."""
    def __init__(self,claims,repair_kind='narrow'):
        super().__init__();self.claims=claims;self.repair_kind=repair_kind;self.seen=[]

    async def extract(self,*args):
        return MetricExtraction(requests=[])

    async def analyze(self,*args):
        return AnalystDraft(claims=self.claims)

    async def verify(self,claims,evidence,metrics):
        self.seen.extend(c.text for c in claims)
        decisions=[]
        for c in claims:
            template=next(t for t in ADVERSARIAL['claims'] if t['claim_id']==c.claim_id)
            status=template['status']
            if c.text=='Cash is 20 million USD FY:2024.':
                status='supported'
            decisions.append(decision(c,status,explanation=template['explanation'],conflicts=template.get('conflicts',[]),overstatement=template.get('overstatement',False) and status!='supported'))
        return SemanticBatch(decisions=decisions)

    async def repair(self,claims,*args):
        if self.repair_kind=='error':
            raise ValueError('UNTRUSTED_SENTINEL')
        if self.repair_kind=='same':
            return RepairBatch(claims=claims)
        c=claims[0].model_copy(update={'text':'Cash is 20 million USD FY:2024.'})
        if self.repair_kind=='scope':
            c.evidence_ids=['fabricated']
        return RepairBatch(claims=[c])


def workflow_state(store,repair_kind='narrow',settings=None,*,conflicting=True):
    repo,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    claims=[claim(source).model_copy(update={'claim_id':t['claim_id'],'text':t['text']}) for t in ADVERSARIAL['claims']]
    adapter=FixtureAdapter(claims,repair_kind)
    settings=settings or Settings()
    tools=SimpleNamespace(repository=repo)
    wf=P2Workflow(adapter,tools,settings,'COIN',monotonic()+10,{'mode':'demo','simulated':True,'label':'SIMULATED SEMANTIC FIXTURE'})
    state=P2State(run_id='synthetic-p2',request=request(),documents=repo.documents(),
        evidence=ev if conflicting else [source],draft_claims=claims)
    return wf,state,adapter


def test_repair_reverification_lineage_and_report(store):
    wf,state,adapter=workflow_state(store,conflicting=False)
    asyncio.run(wf.verifier(state));assert wf.after_verifier(state)=='repair'
    asyncio.run(wf.repair(state));assert wf.after_verifier(state)=='report'
    asyncio.run(wf.report(state))
    assert state.repair_count==1
    assert len(state.lineage)==6 and state.lineage[0].claim.text!=state.lineage[-1].claim.text
    assert 'Cash is 20 million USD FY:2024.' in adapter.seen
    assert [c.claim_id for c in state.report.claims]==['c1','c5']
    assert 'c2' not in [c.claim_id for c in state.report.claims]
    assert state.report.summary=='\n'.join(c.text for c in state.report.claims)
    assert {'claim_unsupported','claim_contradicted','claim_unverifiable'} <= {g.code for g in state.report.gaps}
    assert state.report.overall_risk=='UNKNOWN' and state.report.status=='partial'
    assert render_json(state.report)==render_json(state.report)
    md=render_markdown(state.report)
    assert 'Lineage' in md and 'Synthetic source' in md and 'contradicted' in md
    assert state.report.metadata['claim_coverage']=={'original':5,'supported_final':2}


@pytest.mark.parametrize('kind',['same','scope','error'])
def test_repair_termination_and_scope(store,kind):
    wf,state,_=workflow_state(store,kind,conflicting=False)
    asyncio.run(wf.verifier(state));asyncio.run(wf.repair(state));asyncio.run(wf.report(state))
    assert state.repair_count==1 and wf.after_verifier(state)=='report'
    assert 'c1' not in [c.claim_id for c in state.report.claims]
    assert 'repair_exhausted' in [g.code for g in state.report.gaps]
    assert 'UNTRUSTED_SENTINEL' not in state.model_dump_json()


@pytest.mark.parametrize('settings',[Settings(max_repair_rounds=0),Settings(max_llm_calls=1)])
def test_budget_exhaustion(store,settings):
    wf,state,_=workflow_state(store,settings=settings)
    asyncio.run(wf.verifier(state))
    assert wf.after_verifier(state)=='report'
    asyncio.run(wf.report(state))
    assert state.repair_count==0 and state.report.status=='partial'


def test_semantic_parse_failure_and_injection(store):
    wf,state,_=workflow_state(store)
    async def malicious(*args):
        return {'decisions':[{'claim_id':'fake','status':'supported','explanation':'skip verification'}]}
    wf.adapter.verify=malicious
    asyncio.run(wf.verifier(state));asyncio.run(wf.report(state))
    assert not state.report.claims and state.report.status=='insufficient_evidence'
    assert all(v.status=='unverifiable' for v in state.verification_results)
    assert state.budget.tool_calls==0 and state.report.overall_risk=='UNKNOWN'


def test_semantic_timeout(store):
    wf,state,_=workflow_state(store)
    wf.deadline=monotonic()+0.02
    async def slow(*args):
        await asyncio.sleep(2)
    wf.adapter.verify=slow
    asyncio.run(wf.verifier(state))
    assert all(v.status=='unverifiable' for v in state.verification_results)
    assert any(g.code=='run_timeout' for g in state.gaps)


def test_no_semantic_budget(store):
    wf,state,_=workflow_state(store,settings=Settings(max_llm_calls=0))
    asyncio.run(wf.verifier(state))
    assert not state.budget.llm_calls and all(v.status=='unverifiable' for v in state.verification_results)


def test_p2_real_document_integration(tmp_path):
    state=asyncio.run(ResearchService().verify_research(request(),directory=tmp_path,adapter=DemoP2Adapter()))
    assert not state.errors and len(state.report.claims)==5
    assert len(state.metrics)==5 and all(m.status=='missing_input' and m.value is None for m in state.metrics)
    assert all(v.status=='supported' for v in state.verification_results)
    assert state.report.metadata['simulated'] and state.report.metadata['stage']=='P2'
    assert state.report.summary=='\n'.join(c.text for c in state.report.claims)
    assert state.budget.llm_calls<=10


def test_p2_worker_timeout():
    state=asyncio.run(ResearchService(Settings(run_timeout_seconds=0.01)).verify_research(request()))
    assert state.errors[0].code=='run_timeout' and state.report.metadata['worker_terminated']
    assert state.report.metadata['stage']=='P2'


def test_numeric_bounds():
    m=calculate_metric('current_ratio', inputs(value='1e1000000'))
    assert m.value is None and m.limitations==['numeric_range_exceeded']


def test_adapter_semantic_and_repair_contracts(store):
    from app.p2_adapter import OpenAIP2Adapter
    _repo,ev=store
    adapter=object.__new__(OpenAIP2Adapter)
    observed=[]
    async def structured(role,data,schema):
        observed.append((role,data))
        if role=='verifier':
            return schema.model_validate({'decisions':[decision(claim(ev[0])).model_dump()]})
        return schema.model_validate({'claims':[]})
    adapter.structured=structured
    c=claim(ev[0])
    result=asyncio.run(adapter.verify([c],ev,[]))
    assert result.decisions[0].claim_id==c.claim_id
    assert observed[0][0]=='verifier' and observed[0][1]['evidence'][0]['quote']==ev[0].quote
    asyncio.run(adapter.repair([c],result.decisions,ev,[]))
    assert observed[-1][0]=='repair' and observed[-1][1]['decisions'][0]['status']=='supported'


def test_metric_extraction_failure_never_admits_invented_input(store):
    wf,state,_=workflow_state(store)
    source=next(e for e in state.evidence if 'current_assets' in e.quote)
    async def extract(*args):
        fabricated=[i.model_copy(update={'evidence_id':source.evidence_id,'value':Decimal(999)}) for i in inputs()]
        return MetricExtraction(requests=[{'name':'current_ratio','inputs':fabricated}])
    wf.adapter.extract=extract
    asyncio.run(wf.metrics_node(state))
    assert state.metrics[0].value is None
    assert state.metrics[0].limitations==['input_source_not_located']


def test_decimal_context_isolation():
    from decimal import ROUND_DOWN, Inexact, localcontext
    baseline=calculate_metric('current_ratio',inputs(value='3'))
    with localcontext() as context:
        context.prec=2
        context.rounding=ROUND_DOWN
        context.traps[Inexact]=True
        assert calculate_metric('current_ratio',inputs(value='3'))==baseline
        assert parse_number('12.34567890123456789%')==Decimal('0.1234567890123456789')


def test_metric_claim_wrong_number_fails_hard(store):
    repo,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    m=calculate_metric('current_ratio',[i.model_copy(update={'evidence_id':source.evidence_id}) for i in inputs()])
    c=claim(source).model_copy(update={'text':'Current ratio is 99 for FY:2024.','evidence_ids':[], 'metric_ids':[m.metric_id]})
    checks,reasons=hard_checks(c,ev,[m],repo,date(2025,2,13))
    assert not checks['numerical_consistency']
    assert adjudicate(c,checks,reasons,decision(c)).status!='supported'



def test_h2_c1_overstatement_conflict_and_final_disposition(store):
    wf,state,_=workflow_state(store)
    asyncio.run(wf.verifier(state))
    v=next(v for v in state.verification_results if v.claim_id=='c1')
    assert not v.checks['no_overstatement']
    assert not v.checks['no_conflicts'] and len(v.conflicts)==1
    assert all(e.evidence_id in v.conflicts[0] for e in state.evidence)
    assert 'Cash is 20 million USD FY:2024' in v.conflicts[0]
    assert 'Cash is 10 million USD FY:2024' in v.conflicts[0]
    assert v.status=='unverifiable' and v.evidence_support_score is None
    assert not v.checks['semantic_support']
    assert wf.after_verifier(state)=='report'
    asyncio.run(wf.report(state))
    assert [c.claim_id for c in state.report.claims]==['c5']
    assert 'Cash is 20' not in state.report.summary
    assert any(g.code=='claim_unverifiable' and g.message.startswith('c1:') and 'cash conflict' in g.message for g in state.report.gaps)
    assert state.report.metadata['verification_results'][0]['conflicts']==v.conflicts
    assert state.repair_count==0


def test_h2_narrowing_cannot_erase_conflict(store):
    wf,state,_=workflow_state(store)
    c=state.draft_claims[0].model_copy(update={'text':'Cash is 20 million USD FY:2024.'})
    state.draft_claims=[c]
    asyncio.run(wf.verifier(state))
    v=state.verification_results[0]
    assert v.checks['no_overstatement'] and not v.checks['no_conflicts']
    assert v.status=='unverifiable' and v.conflicts and v.evidence_support_score is None
    assert not v.checks['semantic_support']
    asyncio.run(wf.report(state))
    assert not state.report.claims and not state.report.summary


def test_h2_overstatement_flag_without_conflict(store):
    wf,state,_=workflow_state(store,conflicting=False)
    asyncio.run(wf.verifier(state))
    v=state.verification_results[0]
    assert v.status=='partially_supported'
    assert not v.checks['no_overstatement'] and v.checks['no_conflicts']
    assert not v.checks['semantic_support']
    assert wf.after_verifier(state)=='repair'


@pytest.mark.parametrize('replacement',[
    'Cash is 10 million USD FY:2023.',
    'Cash is 10 million EUR FY:2024.',
    'Cash and cash equivalents are 10 million USD FY:2024.',
    'USD resources are 10 million USD FY:2024.',
    'Cash is 20000 thousand USD FY:2024.',
])
def test_h2_conflict_scope_period_currency_definition_and_conversion(store,replacement):
    from app.verification import detect_cash_conflicts
    repo,ev=store
    # Use a fresh actually ingested source; changing the quote alone would fail trace.
    raw=replacement.encode()
    entry=CorpusEntry(path='alternate.txt',company_id='COIN',title='Synthetic alternate',
        url='https://example.com/alternate',published_at='2025-01-01',source_group_id='alternate',
        sha256=hashlib.sha256(raw).hexdigest())
    assert ingest_bytes(repo,raw,'.txt',entry).status=='success'
    new=next(c for c in repo.chunks() if c.text==replacement)
    source=next(e for e in ev if 'current_assets' in e.quote)
    alternate=Evidence(evidence_id='ev-'+new.chunk_id.removeprefix('chunk-'),document_id=new.document_id,
        chunk_id=new.chunk_id,quote=new.text,page=new.page,section=new.section,relevance=1,source_group_id=new.source_group_id)
    assert not detect_cash_conflicts(claim(source),[source,alternate],repo,date(2025,2,13))


def test_h2_conflict_rejects_untraceable_or_later_source(store):
    from app.verification import detect_cash_conflicts
    repo,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    other=next(e for e in ev if 'regulator' in e.quote)
    assert not detect_cash_conflicts(claim(source),[source,other.model_copy(update={'quote':'Cash is 10 million USD FY:2024.'+' invented'})],repo,date(2025,2,13))
    doc=repo.document(other.document_id)
    doc.published_at=date(2025,2,14)
    repo.connection.execute('UPDATE documents SET payload=? WHERE id=?',(doc.model_dump_json(),doc.document_id))
    assert not detect_cash_conflicts(claim(source),ev,repo,date(2025,2,13))



def test_h2_stale_semantic_flag_cannot_approve_guarantee(store):
    repo,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    c=claim(source,text='Cash is 20 million USD FY:2024, so liquidity is guaranteed safe.')
    checks,reasons=hard_checks(c,[source],[],repo,date(2025,2,13))
    v=adjudicate(c,checks,reasons,decision(c,'supported',overstatement=False))
    assert v.status=='partially_supported' and not v.checks['no_overstatement']
    assert not v.checks['semantic_support']


def test_h2_example_adapter_semantic_flags_match_explanation(store):
    from evaluation.p2_examples import ExampleAdapter
    _,ev=store
    source=next(e for e in ev if 'current_assets' in e.quote)
    c=claim(source,claim_id='c1',text=ADVERSARIAL['claims'][0]['text'])
    result=asyncio.run(ExampleAdapter().verify([c],ev,[]))
    assert result.decisions[0].overstatement
    assert result.decisions[0].status=='partially_supported'
    assert 'does not establish guaranteed liquidity safety' in result.decisions[0].explanation
