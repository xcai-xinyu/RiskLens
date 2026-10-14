"""Independent scoring/protocol tests; no model performance gold tuning."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from evaluation.repository import EvaluationRepository
from evaluation.runner import FixtureAdapter, digest, load_dataset
from evaluation.scorers import aggregate, ratio, score

ROOT=Path(__file__).resolve().parent.parent


def case():
    return {'judgments':[{'text':'Cash is 20 million USD FY:2024.','verdict':'supported','evidence_ids':['e1']},
                        {'text':'Cash is 900 million USD FY:2024.','verdict':'contradicted','evidence_ids':[]}],
        'reference_claims':[{'text':'Cash is 20 million USD FY:2024.','claim_type':'fact','evidence_ids':['e1']}],
        'reference_metrics':[],'expected_abstention':False,'expected_gaps':[],'annotation_status':'PROPOSED_H4'}


def output(text='Cash is 20 million USD FY:2024.',evidence=None):
    return {'status':'completed','claims':[{'text':text,'claim_type':'fact','evidence_ids':['e1'] if evidence is None else evidence}],
            'metrics':[],'gaps':[]}


def test_independent_score_ignores_self_verifier():
    result=output('Cash is 900 million USD FY:2024.');result['verification_results']=[{'status':'supported'}]
    scored=score(case(),result,{'e1'})['metrics']
    assert scored['unsupported_rate']['value']==1
    assert scored['citation_correctness']['value']==0
    assert scored['completeness']['value']==0


def test_empty_denominators_are_na_and_completeness_not_rewarded():
    empty=dict(output(),claims=[])
    scored=score(case(),empty,{'e1'})['metrics']
    assert scored['citation_coverage']['value'] is None
    assert scored['unsupported_rate']['value'] is None
    assert scored['completeness']['value']==0
    assert scored['answerable_false_abstention']['value']==1
    assert ratio(0,0)['status']=='N/A'


def test_unknown_paraphrases_not_false_gold_or_silent_success():
    result=score(case(),output('The cash amount appears substantial.'),{'e1'})
    assert len(result['unresolved_claims'])==1
    assert result['metrics']['citation_correctness']['denominator']==0
    assert result['metrics']['completeness']['value']==0


def test_citation_resolvability_and_entailment_separate():
    result=score(case(),output(evidence=['invented']),{'e1'})['metrics']
    assert result['citation_coverage']['value']==0
    assert result['citation_correctness']['value']==0
    assert result['completeness']['value']==0


def test_false_abstention_and_required_gap():
    c=case();c['expected_abstention']=True;c['expected_gaps']=['missing'];c['reference_claims']=[]
    result=dict(output(),claims=[])
    assert score(c,result,{'e1'})['metrics']['abstention_accuracy']['value']==0
    result['gaps']=['missing']
    assert score(c,result,{'e1'})['metrics']['abstention_accuracy']['value']==1


@pytest.mark.parametrize('changed',['value','formula','unit','currency','period','evidence_id'])
def test_numeric_accuracy_requires_entire_trace(changed):
    item={'name':'current_assets','value':'200','unit':'million','currency':'USD','period':'FY:2024','evidence_id':'e1'}
    ref={'name':'current_ratio','value':'2','formula':'current_assets / current_liabilities','status':'success','inputs':[item]}
    c=case();c['reference_metrics']=[ref];result=output();result['metrics']=[copy.deepcopy(ref)]
    assert score(c,result,{'e1'})['metrics']['numerical_accuracy']['value']==1
    if changed in ('value','formula'):result['metrics'][0][changed]='999'
    else:result['metrics'][0]['inputs'][0][changed]='wrong'
    assert score(c,result,{'e1'})['metrics']['numerical_accuracy']['value']==0


def test_failed_cases_retained_in_coverage():
    result=dict(output(),status='failed')
    scored=score(case(),result,{'e1'})
    assert scored['metrics']=={}
    record={'arm':'B','status':'failed','score':scored,'latency_seconds':1,'llm_calls':1}
    combined=aggregate([record])
    assert combined['B']['failed']==1 and combined['B']['cases']==1
    assert combined['B']['cost'] is None and combined['B']['token_usage'] is None


def test_frozen_dataset_hashes_split_and_annotation_scope():
    manifest,cases,snapshots=load_dataset(ROOT/'evaluation/datasets/p4-v2')
    assert len(cases)==30 and len(snapshots)==30
    assert sum(c['split']=='development' for c in cases)==10
    assert sum(c['split']=='heldout' for c in cases)==20
    assert not manifest['human_approved'] and manifest['annotation_status']=='PROPOSED_H4'
    assert all(c['source_kind']=='synthetic' for c in cases if c['split']=='heldout')
    assert sum(len(c['reference_metrics']) for c in cases)==20


def test_fixture_raw_has_no_evidence_and_no_quality_claim():
    from app.schemas import ResearchRequest
    adapter=FixtureAdapter({})
    request=ResearchRequest(company='Fixture',question='Cash?',as_of_date='2025-01-01',risk_categories=['liquidity'],mode='demo')
    assert asyncio.run(adapter.raw(request)).claims==[]
    assert adapter.calls==1


def test_result_repository_immutable_and_integrity(tmp_path):
    repo=EvaluationRepository(tmp_path)
    result={'mode':'demo','simulated':True,'label':'SYNTHETIC','annotation_status':'PROPOSED_H4',
        'real_model_quality_status':'BLOCKED','records':[],'aggregate':{}}
    repo.save('review-one',result)
    assert repo.get('review-one')['simulated'] and len(repo.history())==1
    with pytest.raises(FileExistsError):repo.save('review-one',result)
    with pytest.raises(ValueError):repo.get('../escape')
    (tmp_path/'review-one/result.json').write_text('{}')
    with pytest.raises(ValueError,match='integrity'):repo.get('review-one')


def test_pair_snapshot_hash_sensitive_to_draft_evidence_metric():
    original={'draft':[],'evidence':[],'metrics':[]}
    assert digest(original)==digest(copy.deepcopy(original))
    for field in original:
        changed=copy.deepcopy(original);changed[field]=['altered']
        assert digest(changed)!=digest(original)


def test_evaluation_api_shared_repository(tmp_path):
    import httpx

    from api.main import create_app
    from app.service import ResearchService
    result={'mode':'demo','simulated':True,'label':'DEMO / SYNTHETIC','annotation_status':'PROPOSED_H4',
        'real_model_quality_status':'BLOCKED','records':[],'aggregate':{}}
    EvaluationRepository(tmp_path/'eval').save('review-example',result)
    async def run():
        service=ResearchService();app=create_app(service=service,db_path=tmp_path/'runs.sqlite',evaluation_path=tmp_path/'eval')
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/evaluations')).json()['items'][0]['simulated']
            assert (await client.get('/evaluations/review-example')).json()['real_model_quality_status']=='BLOCKED'
            assert (await client.get('/evaluations/missing')).status_code==404
            assert (await client.get('/evaluations/invalid_id')).status_code==409
    asyncio.run(run())


def test_evaluation_ui_displays_measured_fixture_and_provisional_scope(monkeypatch):
    import inspect

    from streamlit.testing.v1 import AppTest

    from frontend.client import APIClient
    inspect.stack()
    import streamlit as st
    st.empty()  # Initialize first-element/REPL detection outside AppTest widget deadline.
    def call(self,method,path,**kwargs):
        if path=='/evaluations':return {'items':[{'evaluation_id':'sample'}]}
        return {'label':'DEMO / SYNTHETIC','annotation_status':'PROPOSED_H4','real_model_quality_status':'BLOCKED',
            'aggregate':{'A':{'cases':1,'failed':0,'metrics':{}}},'protocol':{},'protocol_hash':'fixture',
            'split_aggregate':{},'limitations':['Synthetic only'],'records':[]}
    monkeypatch.setattr(APIClient,'call',call)
    app=AppTest.from_file(str(ROOT/'frontend/app.py'),default_timeout=15).run()
    app.radio[0].set_value('Evaluation').run()
    assert not app.exception
    assert any('SYNTHETIC' in w.value and 'BLOCKED' in w.value for w in app.warning)
    assert any('No model-quality improvement' in w.value for w in app.markdown)


@pytest.fixture(scope='module')
def executed():
    from evaluation.runner import evaluate
    return asyncio.run(evaluate(ROOT/'evaluation/datasets/p4-v2',fault_case='heldout-19'))


def test_executed_pairs_and_bounded_reverification(executed):
    assert len(executed['records'])==150
    assert len(executed['shared_snapshots'])==30
    for snapshot in executed['shared_snapshots']:
        pairs=[r for r in executed['records'] if r['case_id']==snapshot['case_id'] and r['arm'] in ('B','C','D','C_REPAIR')]
        assert {r['shared_snapshot_hash'] for r in pairs}=={snapshot['hash']}
        assert all(r['original_draft']==snapshot['snapshot']['draft'] for r in pairs)
        rule=next(r for r in pairs if r['arm']=='D')
        assert rule['llm_calls']==0 and all(not d['semantic_checked'] for d in rule['decisions'])
        repair=next(r for r in pairs if r['arm']=='C_REPAIR')
        assert repair['repair_count']<=repair['repair_budget']==1
        assert repair['llm_calls']<=repair['llm_call_limit']
        if repair['repair_count']:
            assert any(l['round']==1 for l in repair['lineage'])
    assert executed['quality_improvement_claim'] is None and executed['real_model_quality_status']=='BLOCKED'


def test_fault_record_preserved_without_case_deletion(executed):
    row=next(r for r in executed['records'] if r['case_id']=='heldout-19' and r['arm']=='A')
    assert row['status']=='failed' and row['score']['metrics']=={}
    assert executed['aggregate']['A']['cases']==30 and executed['aggregate']['A']['failed']==1


def test_baseline_failure_does_not_drop_cases_or_block_raw(monkeypatch):
    from evaluation import runner
    manifest,cases,snapshots=load_dataset(ROOT/'evaluation/datasets/p4-v2')
    first=cases[0]
    monkeypatch.setattr(runner,'load_dataset',lambda path:(manifest,[first],snapshots))
    class Broken(FixtureAdapter):
        def __init__(self):super().__init__({})
        async def analyze(self,*args):raise RuntimeError('PRIVATE detail')
    result=asyncio.run(runner.evaluate(ROOT/'evaluation/datasets/p4-v2',mode='offline_research',adapter_factory=Broken))
    assert len(result['records'])==5
    assert next(r for r in result['records'] if r['arm']=='A')['status']=='completed'
    assert all(r['status']=='failed' for r in result['records'] if r['arm']!='A')
    assert result['simulated'] and result['real_model_quality_status']=='BLOCKED'
    assert 'PRIVATE detail' not in json.dumps(result)


def test_incomplete_evaluation_bundle_never_exposed(tmp_path):
    repo=EvaluationRepository(tmp_path)
    (tmp_path/'unfinished').mkdir()
    assert repo.history()==[]
    with pytest.raises(ValueError,match='not_sealed'):repo.get('unfinished')


def test_live_usage_aggregated_only_when_known():
    record={'arm':'B','status':'completed','score':{'metrics':{},'unresolved_claims':[]},
        'latency_seconds':1,'llm_calls':1,'actual_usage':[{'input_tokens':10,'output_tokens':5,'total_tokens':15}]}
    assert aggregate([record])['B']['token_usage']['total_tokens']==15
    record['actual_usage']=[None]
    assert aggregate([record])['B']['token_usage'] is None


def test_total_deadline_terminates_worker_group():
    import sys
    import time

    from evaluation.p4_process import bounded
    start=time.monotonic()
    assert bounded([sys.executable,'-c','import time; time.sleep(60)'],.1)==124
    assert time.monotonic()-start<3
