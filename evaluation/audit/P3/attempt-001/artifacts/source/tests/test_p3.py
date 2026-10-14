"""P3 integration contracts. Mocks are explicitly distinguished from actual P2 runtime."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from api.main import create_app
from app.config import Settings
from app.history import compare_reports
from app.p3_contracts import HistoryQuery
from app.p3_jobs import Jobs
from app.reporting import render_json, render_markdown
from app.schemas import ResearchRequest, RiskReport
from app.service import ResearchService
from app.state import Budget
from app.storage.run_repository import RunRepository
from main import persisted_run

ROOT=Path(__file__).resolve().parent.parent
HISTORY_FIXTURE=json.loads((ROOT/'tests/fixtures/regression/p3_history_v1.json').read_text())
REQUEST=ResearchRequest(company='Coinbase',question='Assess liquidity',as_of_date='2025-02-13',risk_categories=['liquidity'],mode='demo')


def sample_report():
    return RiskReport.model_validate_json((ROOT/'evaluation/audit/P2/attempt-002/artifacts/examples/synthetic_report.json').read_text())


class FixtureService(ResearchService):
    """Surface tests reuse a sealed synthetic P2 report; no model quality claim."""
    def __init__(self,*,status='partial',error=False,hold=False):
        super().__init__(settings=Settings(run_timeout_seconds=5))
        self.calls=0;self.status=status;self.error=error;self.hold=hold
        self.release=asyncio.Event()

    async def verify_research(self,request,**options):
        self.calls+=1
        if self.hold:
            await self.release.wait()
        if self.error:
            raise ValueError('SECRET sentinel should not leak')
        report=sample_report()
        report.status='complete' if self.status=='completed' else 'partial'
        return SimpleNamespace(report=report,errors=[],budget=Budget(tool_calls=2,llm_calls=3),repair_count=0)


async def terminal(client,run_id):
    for _ in range(100):
        response=await client.get('/runs/'+run_id)
        if response.json()['status'] not in ('queued','running'):
            return response.json()
        await asyncio.sleep(.01)
    raise AssertionError('bounded test task failed to terminate')


def test_api_endpoints_and_shared_service(tmp_path):
    async def scenario():
        service=FixtureService();app=create_app(service=service,db_path=tmp_path/'runs.sqlite')
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/health')).json()['status']=='ok'
            assert service.calls==0
            response=await client.post('/research',json=REQUEST.model_dump(mode='json'))
            assert response.status_code==202
            run_id=response.json()['run_id'];run=await terminal(client,run_id)
            assert run['status']=='partial' and run['report_available']
            assert service.calls==1
            report=(await client.get('/reports/'+run_id)).json()
            assert report['run_id']==run_id
            expected=service.get_report(run_id)
            assert (await client.get(f'/reports/{run_id}/export?format=json')).text==render_json(expected)
            assert (await client.get(f'/reports/{run_id}/export?format=markdown')).text==render_markdown(expected)
            assert (await client.get(f'/reports/{run_id}/export?format=csv')).status_code==422
            history=(await client.get('/reports?company=coinbase&as_of_from=2025-01-01&as_of_to=2025-12-31')).json()
            assert history['total']==1 and history['items'][0]['run_id']==run_id
            assert (await client.get('/reports?offset=1')).json()['items']==[]
            assert (await client.get('/reports?company=none')).json()['total']==0
            assert (await client.get('/reports?limit=101')).status_code==422
            assert (await client.get('/reports?as_of_from=2026-01-01&as_of_to=2025-01-01')).status_code==422
            for path in ('/runs/missing','/reports/missing','/reports/missing/export'):
                assert (await client.get(path)).status_code==404
            assert (await client.post('/reports/compare',json={'run_ids':[run_id,'missing']})).status_code==404
            assert (await client.post('/reports/compare',json={'run_ids':[run_id,run_id]})).status_code==409
            assert (await client.post('/reports/compare',json={'run_ids':[run_id]})).status_code==422
            assert (await client.post('/research',json={'company':'Coinbase'})).status_code==422
            bad=REQUEST.model_dump(mode='json');bad['mode']='imaginary'
            assert (await client.post('/research',json=bad)).status_code==422
            assert 'ResearchRequest' in (await client.get('/openapi.json')).json()['components']['schemas']
    asyncio.run(scenario())


def test_pending_queue_capacity_and_interrupted_run_recovery(tmp_path):
    async def scenario():
        service=FixtureService(hold=True);path=tmp_path/'runs.sqlite';app=create_app(service=service,db_path=path,queue_capacity=1)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            first=(await client.post('/research',json=REQUEST.model_dump(mode='json'))).json()['run_id']
            await asyncio.sleep(.01)
            pending=await client.get('/reports/'+first)
            assert pending.status_code==202 and pending.json()['status']=='running'
            assert (await client.get('/reports/'+first+'/export')).status_code==409
            second=(await client.post('/research',json=REQUEST.model_dump(mode='json'))).json()['run_id']
            assert (await client.post('/research',json=REQUEST.model_dump(mode='json'))).status_code==503
            assert service.calls==1  # bounded concurrency, queued second not started
            assert (await client.get('/runs/'+second)).json()['status']=='queued'
            assert (await client.post('/reports/compare',json={'run_ids':[first,second]})).status_code==409
        repo=RunRepository(path)
        for run_id in (first,second):
            run=repo.get(run_id)
            assert run.status=='failed' and run.stage=='interrupted'
            assert run.errors[0].code=='process_interrupted' and not run.report_available
        repo.close()
    asyncio.run(scenario())


def test_failed_and_completed_are_distinct(tmp_path):
    async def scenario():
        for status,error in [('completed',False),('failed',True)]:
            app=create_app(service=FixtureService(status=status,error=error),db_path=tmp_path/(status+'.sqlite'))
            async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                run_id=(await client.post('/research',json=REQUEST.model_dump(mode='json'))).json()['run_id']
                run=await terminal(client,run_id)
                assert run['status']==status
                if error:
                    response=await client.get('/reports/'+run_id)
                    assert response.status_code==409 and response.json()['status']=='failed'
                    assert 'SECRET' not in json.dumps(run)
    asyncio.run(scenario())


def test_sqlite_restart_snapshots_no_chroma_and_history(tmp_path):
    path=tmp_path/'runs.sqlite';repo=RunRepository(path);report=sample_report()
    repo.create(report.run_id,REQUEST,{})
    repo.finish(report.run_id,report,'partial',[],{})
    repo.create('abandoned',REQUEST,{});repo.update('abandoned','running','verifier')
    repo.create('queued-abandoned',REQUEST,{})
    with pytest.raises(ValueError,match='single_instance'):
        RunRepository(path)
    repo.close()
    reopened=RunRepository(path)
    assert set(reopened.recover())=={'abandoned','queued-abandoned'}
    assert reopened.report(report.run_id)==report
    assert reopened.get(report.run_id).status=='partial'
    assert reopened.get('abandoned').stage=='interrupted'
    assert reopened.history(HistoryQuery())['total']==1
    assert reopened.history(HistoryQuery(company="' OR 1=1 --"))['total']==0
    with pytest.raises(sqlite3.IntegrityError):
        reopened.finish(report.run_id,report,'partial',[],{})
    assert reopened.report(report.run_id)==report  # immutable snapshot, no replace
    reopened.close()
    # Persistence implementation must not import/cache document or vector authorities.
    source=(ROOT/'app/storage/run_repository.py').read_text()
    assert 'chromadb' not in source and 'storage.repository import' not in source


def test_non_comparable_history_fixture_and_api(tmp_path):
    async def scenario():
        app=create_app(service=FixtureService(),db_path=tmp_path/'runs.sqlite')
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            repo=app.state.service.run_repository
            a=sample_report();b=a.model_copy(deep=True)
            a.run_id='coverage-a';b.run_id='coverage-b';b.as_of_date=HISTORY_FIXTURE['non_comparable']['right_as_of_date']
            b.sources=[];b.metadata=dict(b.metadata,corpus_hash=HISTORY_FIXTURE['non_comparable']['right_corpus_hash'],model=HISTORY_FIXTURE['non_comparable']['right_model'])
            for report in (a,b):
                repo.create(report.run_id,REQUEST,{});repo.finish(report.run_id,report,'partial',[],{})
            response=await client.post('/reports/compare',json={'run_ids':[a.run_id,b.run_id]})
            assert response.status_code==200
            result=response.json()
            assert not result['directly_comparable'] and result['risk_change_conclusion'] is None
            assert set(HISTORY_FIXTURE['non_comparable']['expected_reasons'])<=set(result['reasons'])
            assert result['left']['as_of_date']!=result['right']['as_of_date']
            for side in ('left','right'):
                assert all(field in result[side] for field in ('corpus_version','model','prompt_version','rubric_version','sources'))
    asyncio.run(scenario())


def test_comparison_never_guesses_trend():
    a=sample_report();b=a.model_copy(deep=True)
    result=compare_reports(a,b)
    assert not result['directly_comparable'] and result['risk_change_conclusion'] is None
    a.status=b.status='complete'
    assert compare_reports(a,b)['directly_comparable']
    b.metadata['model']=None
    assert 'model_unknown' in compare_reports(a,b)['reasons']


def test_cli_same_service_boundary(tmp_path):
    service=FixtureService()
    state=asyncio.run(persisted_run(service,REQUEST,tmp_path/'cli.sqlite'))
    assert service.calls==1 and state.report and not state.errors
    repo=RunRepository(tmp_path/'cli.sqlite')
    assert repo.report(state.report.run_id)==state.report
    repo.close()
    for path in ('api/main.py','frontend/app.py','frontend/client.py'):
        source=(ROOT/path).read_text()
        for forbidden in ('calculate_metric(', 'verify_claims(', 'hard_checks(', 'langgraph', 'sqlite3'):
            assert forbidden not in source
    assert 'ResearchService' in (ROOT/'api/main.py').read_text()
    assert 'APIClient' in (ROOT/'frontend/app.py').read_text()


@pytest.mark.parametrize('capacity',[0,-1,101])
def test_queue_bound_validation(capacity):
    with pytest.raises(ValueError,match='bounded'):
        Jobs(FixtureService(),None,capacity)


def test_ui_smoke(monkeypatch):
    """AppTest widget execution with explicit synthetic HTTP-response fixture."""
    # Streamlit 1.65 detects REPL via inspect.stack on first element. Populate
    # Python source/module lookup before the widget runner deadline on WSL /mnt/c.
    import inspect

    from streamlit.testing.v1 import AppTest
    inspect.stack()

    from frontend.client import APIClient
    report=sample_report().model_dump(mode='json')
    ids=['review-a','review-b'];calls=[]
    def fake_call(self,method,path,**kwargs):
        calls.append((method,path,kwargs))
        if path=='/research':
            return {'run_id':ids[0],'status':'queued'}
        if path.startswith('/runs/'):
            return {'status':'partial','stage':'report','budget':{'used':{'llm_calls':3}},'errors':[],'report_available':True}
        if path=='/reports/compare':
            other=sample_report();other.sources=[]
            return compare_reports(sample_report(),other)
        if path=='/reports':
            return {'total':2,'items':[{'run_id':i,'company':'Coinbase','as_of_date':'2025-02-13','status':'partial','overall_risk':'UNKNOWN'} for i in ids]}
        return report
    monkeypatch.setattr(APIClient,'call',fake_call)
    app=AppTest.from_file(str(ROOT/'frontend/app.py'),default_timeout=15).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    assert calls[0][:2]==('POST','/research')
    assert any(e.label.startswith('Citation ·') for e in app.expander)
    assert any(e.label.startswith('Calculation ·') for e in app.expander)
    assert {'Risk Overview','Risk Factors','Claims & Evidence','Metrics','Gaps & Conflicts'}<={e.value for e in app.subheader}
    assert any('UNKNOWN means' in e.value and 'LOW' in e.value for e in app.caption)
    assert any('not a failed' in e.value for e in app.caption)
    # AppTest renders expander contents (including original quotes/formula/inputs).
    assert any('USD' in e.value and 'FY:2024' in e.value for e in app.text)
    assert any('Cash' in e.value for e in app.text)
    app.radio[0].set_value('Report History').run()
    assert not app.exception
    app.multiselect[0].set_value(ids).run()
    next(b for b in app.button if b.label=='Compare reports').click().run()
    assert not app.exception
    assert any('Not directly comparable' in e.value for e in app.warning)
    app.radio[0].set_value('Evaluation').run()
    assert any('Planned for P4' in e.value for e in app.info)


def test_ui_failure_is_not_high_risk(monkeypatch):
    import inspect

    from streamlit.testing.v1 import AppTest

    from frontend.client import APIClient
    inspect.stack()
    def failed(self,method,path,**kwargs):
        if path=='/research':
            return {'run_id':'failed-run','status':'queued'}
        return {'status':'failed','stage':'failed','budget':{'used':None},'errors':[{'code':'research_failed','message':'Provider unavailable'}],'report_available':False}
    monkeypatch.setattr(APIClient,'call',failed)
    app=AppTest.from_file(str(ROOT/'frontend/app.py'),default_timeout=15).run()
    app.button[0].click().run()
    assert not app.exception
    assert any('not a HIGH risk' in e.value for e in app.error)
    assert not any(e.value=='Risk Overview' for e in app.subheader)


def test_ui_low_and_unknown_have_different_colors():
    from frontend.app import RISK_COLOR
    assert RISK_COLOR['LOW']!=RISK_COLOR['UNKNOWN']
    assert len(set(RISK_COLOR.values()))==4


def test_api_client_real_http_contract_and_redaction():
    from frontend.client import APIClient, APIError
    calls=[]
    def handler(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/health':
            return httpx.Response(200,json={'status':'ok'})
        if request.url.path.endswith('/export'):
            return httpx.Response(200,content=b'persisted export')
        return httpx.Response(500,json={'detail':'PRIVATE error'})
    client=APIClient('http://test',transport=httpx.MockTransport(handler))
    assert client.call('GET','/health')=={'status':'ok'}
    assert client.call('GET','/reports/a/export',export=True)==b'persisted export'
    with pytest.raises(APIError) as error:
        client.call('GET','/failed')
    assert 'PRIVATE' not in str(error.value)
    assert calls[0]==('GET','/health')
