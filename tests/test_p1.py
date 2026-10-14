import asyncio
import hashlib
import json
import socket
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.config import Settings
from app.p1_adapter import DemoResearchAdapter
from app.p1_contracts import AnalystDraft, CorpusEntry, ResearchTurn, ToolCall
from app.rag.ingestion import MAX_BYTES, chunk_sections, ingest_bytes, ingest_file
from app.rag.retriever import Retriever
from app.schemas import Evidence, ResearchRequest, ToolResult
from app.service import ResearchService
from app.storage.repository import Repository
from app.tools.network import FetchError, fetch_public, public_target
from app.tools.research import ResearchTools

ROOT = Path(__file__).resolve().parent.parent


def request(cutoff='2025-02-13', **changes):
    values = {'company':'Coinbase','question':'Analyze capital liquidity USD resources and SEC regulatory appeal discovery',
                  'as_of_date':cutoff,'risk_categories':['liquidity','regulatory'],'mode':'demo'}
    values.update(changes)
    return ResearchRequest.model_validate(values)


def entry(raw, **changes):
    values = {'path':'test.txt','company_id':'COIN','title':'Synthetic test','url':'https://example.com/test',
                  'published_at':'2025-01-01','source_group_id':'original-1','sha256':hashlib.sha256(raw).hexdigest()}
    values.update(changes)
    return CorpusEntry.model_validate(values)


def run(directory, **options):
    options.setdefault('adapter', DemoResearchAdapter())
    return asyncio.run(ResearchService(options.pop('settings',None)).research(
        options.pop('request',request()),directory=directory,**options))


@pytest.fixture(scope='module')
def fixed_store(tmp_path_factory):
    directory=tmp_path_factory.mktemp('p1-real-source')
    repo=Repository(directory)
    manifest=json.loads((ROOT/'data/sample/corpus_v1.json').read_text())
    result=ingest_file(repo,ROOT/'data/sample',CorpusEntry.model_validate(manifest['documents'][0]))
    assert result.status=='success'
    retriever=Retriever(repo,directory/'chroma')
    asyncio.run(retriever.rebuild())
    yield repo,retriever
    repo.close()


def test_real_pdf_ingestion_and_trace(fixed_store):
    repo,retriever=fixed_store
    assert len(repo.documents())==1 and len(repo.chunks())>=24
    assert {c.page for c in repo.chunks()}==set(range(1,25))
    result=asyncio.run(retriever.search('capital liquidity USD resources','COIN',date(2025,2,13),12))
    assert result.status=='success'
    for raw in result.data:
        evidence=Evidence.model_validate(raw)
        assert repo.trace(evidence,date(2025,2,13))['quote_located']
    assert any('At the end of Q4 we had $9.3 billion' in raw['quote'] for raw in result.data)


def test_date_filter_unknown_dates_and_future(tmp_path):
    repo=Repository(tmp_path)
    for index,published in enumerate(('2024-12-31','2025-02-14',None)):
        raw=f'Cash liquidity source {index}'.encode()
        assert ingest_bytes(repo,raw,'.txt',entry(raw,url=f'https://example.com/{index}',published_at=published)).status=='success'
    retriever=Retriever(repo,tmp_path/'chroma')
    asyncio.run(retriever.rebuild())
    result=asyncio.run(retriever.search('cash liquidity','COIN',date(2025,2,13),12))
    assert len(result.data)==1
    doc=repo.document(result.data[0]['document_id'])
    assert doc.published_at==date(2024,12,31)
    assert len(repo.documents())==3
    assert asyncio.run(retriever.search('cash','COIN',date(2024,1,1),12)).status=='empty'
    repo.close()


def test_dedup_and_same_source_group(tmp_path):
    repo=Repository(tmp_path)
    raw=b'Cash resources liquidity original release'
    assert ingest_bytes(repo,raw,'.txt',entry(raw)).status=='success'
    duplicate=ingest_bytes(repo,raw,'.txt',entry(raw,url='https://example.com/reprint'))
    assert duplicate.provenance['deduplicated'] is True
    assert len(repo.documents())==1
    other=b'Cash resources liquidity original release additional words'
    ingest_bytes(repo,other,'.txt',entry(other,url='https://example.com/reprint2'))
    retriever=Retriever(repo,tmp_path/'chroma')
    asyncio.run(retriever.rebuild())
    result=asyncio.run(retriever.search('cash liquidity','COIN',date(2025,2,13),12))
    assert {e['source_group_id'] for e in result.data}=={'original-1'}
    assert len({e['evidence_id'] for e in result.data})==len(result.data)
    repo.close()


def test_index_rebuild_preserves_authority(fixed_store):
    repo,retriever=fixed_store
    before=asyncio.run(retriever.search('capital liquidity','COIN',date(2025,2,13),12)).data
    asyncio.run(retriever.rebuild())
    after=asyncio.run(retriever.search('capital liquidity','COIN',date(2025,2,13),12)).data
    assert before==after
    assert all(repo.trace(Evidence.model_validate(e),date(2025,2,13)) for e in after)


@pytest.mark.parametrize('change,expected', [({'evidence_id':'forged'},'citation_id_invalid'),
    ({'document_id':'forged'},'citation_id_invalid'),({'chunk_id':'forged'},'citation_id_invalid'),
    ({'page':999},'citation_location_invalid'),({'quote':'not in original source'},'citation_quote_invalid')])
def test_citation_trace_rejects_invalid_locations(fixed_store,change,expected):
    repo,retriever=fixed_store
    raw=asyncio.run(retriever.search('capital liquidity','COIN',date(2025,2,13),1)).data[0]
    evidence=Evidence.model_validate(raw|change)
    with pytest.raises(ValueError,match=expected):
        repo.trace(evidence,date(2025,2,13))


def test_chunk_boundaries_and_overlap():
    sections=[(1,None,' '.join('token'+str(i) for i in range(1500))),
              (2,None,'Second page quote')]
    chunks=chunk_sections('doc-test',sections,'COIN','original')
    assert len(chunks)==4
    assert chunks[0].text.split()[-120:]==chunks[1].text.split()[:120]
    assert chunks[-1].page==2 and chunks[-1].text=='Second page quote'
    assert all(c.start<c.end for c in chunks)


def test_parser_empty_error_html_and_ocr(tmp_path):
    repo=Repository(tmp_path)
    assert ingest_bytes(repo,b'','.txt',entry(b'')).status=='empty'
    assert ingest_bytes(repo,b'%PDF-invalid','.pdf',entry(b'%PDF-invalid')).error_code=='parse_error'
    writer=PdfWriter();writer.add_blank_page(width=100,height=100)
    from io import BytesIO
    buf=BytesIO();writer.write(buf);raw=buf.getvalue()
    assert ingest_bytes(repo,raw,'.pdf',entry(raw)).error_code=='needs_ocr'
    html=b'<html><script>execute_code()</script><h1>Cash</h1><p>Liquidity quote.</p></html>'
    result=ingest_bytes(repo,html,'.html',entry(html,url='https://example.com/html'))
    assert result.status=='success'
    chunk=repo.chunks()[0]
    assert chunk.page is None and chunk.section=='HTML body'
    assert 'execute_code' not in chunk.text
    repo.close()


def test_upload_paths_types_sizes_hash(tmp_path):
    repo=Repository(tmp_path/'store')
    for path in ('../escape.txt','.env','bad.exe'):
        assert ingest_file(repo,tmp_path,entry(b'',path=path)).error_code=='upload_path_or_type_invalid'
    assert ingest_bytes(repo,b'x'*(MAX_BYTES+1),'.txt',entry(b'x')).error_code=='document_too_large'
    assert ingest_bytes(repo,b'different','.txt',entry(b'expected')).error_code=='source_hash_mismatch'
    repo.close()


def test_full_demo_claims_and_no_network(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Demo must not use sockets')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    state=run(tmp_path)
    assert not state.errors
    assert len(state.draft_claims)==5
    assert {c.claim_type for c in state.draft_claims}=={'fact','inference'}
    known={e.evidence_id for e in state.evidence}
    assert all(set(c.evidence_ids)<=known for c in state.draft_claims)
    assert state.budget.tool_calls==1 and state.budget.llm_calls==4
    assert state.report.claims==[] and state.report.overall_risk=='UNKNOWN'
    assert state.metadata['simulated'] and 'REAL FIXED DOCUMENT' in state.metadata['label']
    assert state.report.status=='partial'
    repo=Repository(tmp_path)
    assert all(repo.trace(e,state.request.as_of_date) for e in state.evidence)
    repo.close()


def test_before_publication_runtime(tmp_path):
    state=run(tmp_path,request=request('2025-02-12'))
    assert state.evidence==[] and state.draft_claims==[]
    assert state.report.status=='insufficient_evidence'
    assert {'after_cutoff','empty_result'}<={g.code for g in state.gaps}


@pytest.mark.parametrize('config,expected', [({'max_tool_calls':0},'tool_budget_exhausted'),
    ({'max_llm_calls':0},'llm_budget_exhausted'),({'max_llm_calls':2},'llm_budget_exhausted')])
def test_budget_terminates_p1(tmp_path,config,expected):
    state=run(tmp_path,settings=Settings(**config))
    assert expected in {g.code for g in state.errors}
    assert state.budget.tool_calls<=state.metadata['config']['max_tool_calls']
    assert state.budget.llm_calls<=state.metadata['config']['max_llm_calls']


def test_repeated_calls_terminate(tmp_path):
    class Repeated(DemoResearchAdapter):
        async def research_turn(self,context):
            return ResearchTurn(action='tool',tool_call=ToolCall(name='search_documents',arguments={
                'query':'cash liquidity','company_id':'COIN','as_of_date':'2025-02-13'}))
    state=run(tmp_path,adapter=Repeated())
    assert state.budget.tool_calls==1
    assert 'repeated_tool_call' in {g.code for g in state.errors}


def test_analyst_fabricated_id_rejected(tmp_path):
    class FakeAnalyst(DemoResearchAdapter):
        async def analyze(self,request,evidence,metrics):
            return AnalystDraft.model_validate({'claims':[{'claim_id':'invented','text':'Unsupported',
                'claim_type':'fact','risk_category':'liquidity','evidence_ids':['invented-evidence'],'metric_ids':[]}]})
    state=run(tmp_path,adapter=FakeAnalyst())
    assert state.draft_claims==[]
    assert 'claim_citation_invalid' in {g.code for g in state.errors}


def test_prompt_injection_cannot_grant_tools(tmp_path,monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY","PRIVATE_KEY_SENTINEL_DO_NOT_DISCLOSE")
    raw=(ROOT/'tests/fixtures/regression/injection_v1.txt').read_bytes()
    corpus=tmp_path/'input';corpus.mkdir();(corpus/'test.txt').write_bytes(raw)
    (corpus/'manifest.json').write_text(json.dumps({'version':'adversarial-v1',
        'company_aliases':{'coinbase':'COIN'},'documents':[entry(raw).model_dump()]}))
    class Attack(DemoResearchAdapter):
        async def research_turn(self,context):
            count=len(context['tool_events'])
            if count==0:
                return ResearchTurn(action='tool',tool_call=ToolCall(name='search_documents',arguments={
                    'query':'cash liquidity','company_id':'COIN','as_of_date':'2025-02-13'}))
            if count==1:
                assert 'fake tool return' in json.dumps(context).lower()
                return ResearchTurn(action='tool',tool_call=ToolCall(name='run_shell',arguments={'command':'read .env'}))
            return ResearchTurn(action='finish',tool_call=None)
    state=run(tmp_path/'store',corpus=corpus/'manifest.json',adapter=Attack())
    assert state.tool_events[1].result.error_code=='unauthorized_tool'
    assert state.report.overall_risk=='UNKNOWN' and not state.report.claims
    assert not state.draft_claims
    # Attack text stays quoted data, never a credential disclosure or executed operation.
    assert state.budget.tool_calls==2 and state.budget.tool_calls<=12
    assert 'PRIVATE_KEY_SENTINEL_DO_NOT_DISCLOSE' not in state.model_dump_json()


@pytest.mark.parametrize('url',['http://localhost/','http://127.0.0.1/','http://169.254.169.254/',
    'http://10.0.0.1/','http://[::1]/','file:///etc/passwd','https://user:pass@example.com/',
    'https://example.com:8443/','http://localhost.evil/'])
def test_ssrf_targets_rejected(url):
    def private_resolver(*args,**kwargs):
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
    with pytest.raises(FetchError):
        public_target(url,resolver=private_resolver)


def test_public_target_pin_and_redirect_guard(monkeypatch):
    def resolver(host,port,**kwargs):
        ip='127.0.0.1' if host=='localhost' else '93.184.216.34'
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port))]
    # public_target's default resolver is patched explicitly for fetch_public as well.
    from app.tools import network
    original=network.public_target
    monkeypatch.setattr(network,'public_target',lambda url:original(url,resolver))
    assert original('https://example.com/x',resolver)[3]=='93.184.216.34'
    calls=[]
    def redirect(url,timeout):
        calls.append(url);return 302,{'Location':'http://localhost/secrets'},b''
    with pytest.raises(FetchError):
        fetch_public('https://example.com',transport=redirect)
    assert len(calls)==1
    def oversize(url,timeout):
        return 200,{},b'x'*(MAX_BYTES+1)
    with pytest.raises(FetchError,match='document_too_large'):
        fetch_public('https://example.com',transport=oversize)


def test_tool_scope_and_offline_network(fixed_store):
    repo,retriever=fixed_store
    tools=ResearchTools(repo,retriever,request(),'COIN',12)
    for name,args,code in [('run_shell',{},'unauthorized_tool'),
        ('search_documents',{'query':'cash','company_id':'COIN','as_of_date':'2099-01-01'},'cutoff_override_forbidden'),
        ('search_documents',{'query':'cash','company_id':'OTHER','as_of_date':'2025-02-13'},'company_override_forbidden'),
        ('search_web',{'query':'cash','as_of_date':'2025-02-13'},'network_mode_forbidden'),
        ('fetch_document',{'url':'https://example.com'},'network_mode_forbidden')]:
        result=asyncio.run(tools.execute(ToolCall(name=name,arguments=args)))
        assert result.error_code==code
    empty=asyncio.run(tools.execute(ToolCall(name='search_documents',arguments={
        'query':'zzzznotpresent','company_id':'COIN','as_of_date':'2025-02-13'})))
    assert empty.status=='empty' and empty.error_code is None


def test_missing_real_config_no_demo_fallback(tmp_path,monkeypatch):
    for key in ('OPENAI_API_KEY','LLM_MODEL','EMBEDDING_MODEL','TAVILY_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    for mode in ('offline_research','live'):
        with pytest.raises(ValueError,match='provider_configuration_missing:OPENAI_API_KEY,LLM_MODEL,EMBEDDING_MODEL'):
            run(tmp_path,request=request(mode=mode))


def test_company_ambiguity_no_guess(tmp_path):
    with pytest.raises(ValueError,match='company_unresolved'):
        run(tmp_path,request=request(company='Unknown ambiguous company'))


def test_total_timeout_kills_native_worker(tmp_path):
    state=asyncio.run(ResearchService(Settings(run_timeout_seconds=0.01)).research(
        request(),directory=tmp_path))
    assert state.errors[0].code=='run_timeout'
    assert state.report.metadata['worker_terminated'] is True
    assert state.report.overall_risk=='UNKNOWN'


def test_model_parse_error_p1_redacts_payload(tmp_path):
    class Malformed(DemoResearchAdapter):
        async def make_plan(self,request,company_id):
            return {'invalid':'PRIVATE_TEST_SENTINEL'}
    state=run(tmp_path,adapter=Malformed())
    assert state.draft_claims==[] and state.evidence==[]
    assert state.errors[0].code=='model_parse_error'
    assert 'PRIVATE_TEST_SENTINEL' not in state.model_dump_json()


def test_tool_timeout_is_bounded(fixed_store):
    from time import monotonic

    from app.p1_contracts import P1State
    from app.p1_workflow import P1Workflow
    from app.schemas import ResearchPlan
    repo,_=fixed_store
    class SlowTool:
        repository=repo
        async def execute(self,call):
            await asyncio.sleep(5)
            return ToolResult(status='empty')
    state=P1State(run_id='timeout-test',request=request(),plan=ResearchPlan(
        company_id='COIN',subquestions=[],search_queries=[],required_metrics=[]))
    flow=P1Workflow(DemoResearchAdapter(),SlowTool(),Settings(), 'COIN',monotonic()+0.02,{})
    start=monotonic()
    result=asyncio.run(flow.research(state))
    assert monotonic()-start<1
    assert result['errors'][0]['code']=='run_timeout'
    assert result['budget']['tool_calls']==1


def test_same_url_revision_keeps_distinct_snapshots(tmp_path):
    repo=Repository(tmp_path)
    original=b'Cash disclosure original'
    revised=b'Cash disclosure changed later'
    ingest_bytes(repo,original,'.txt',entry(original,published_at='2024-01-01'))
    ingest_bytes(repo,revised,'.txt',entry(revised,published_at='2026-01-01'))
    assert len(repo.documents())==2
    retriever=Retriever(repo,tmp_path/'chroma')
    asyncio.run(retriever.rebuild())
    result=asyncio.run(retriever.search('Cash disclosure','COIN',date(2025,1,1),12))
    assert len(result.data)==1 and 'original' in result.data[0]['quote']
    repo.close()


def test_executor_completion_progresses_in_restricted_wsl():
    from time import monotonic

    from app.async_helpers import run_sync
    start=monotonic()
    assert asyncio.run(run_sync(lambda: 'completed'))=='completed'
    assert monotonic()-start<1


def test_chroma_does_not_parse_dotenv_files(tmp_path,monkeypatch):
    from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource

    from app.rag.chroma_runtime import ChromaSettings
    # A synthetic dotenv source is forbidden; never inspect the real workspace .env.
    (tmp_path/'synthetic.env').write_text('ALLOW_RESET=true\nTEST_PRIVATE_SENTINEL=fake-value\n')
    monkeypatch.delenv('ALLOW_RESET',raising=False)
    def forbidden(*args,**kwargs):
        pytest.fail('Chroma must not parse any dotenv file')
    monkeypatch.setattr(DotEnvSettingsSource,'_read_env_file',forbidden)
    settings=ChromaSettings(_env_file=tmp_path/'synthetic.env',anonymized_telemetry=False)
    assert settings.anonymized_telemetry is False
    assert settings.allow_reset is False


def test_analyst_does_not_access_repository(fixed_store):
    from time import monotonic

    from app.p1_contracts import P1State
    from app.p1_workflow import P1Workflow
    _,retriever=fixed_store
    result=asyncio.run(retriever.search(request().question,'COIN',date(2025,2,13),12))
    class ForbiddenRepository:
        def trace(self,*args):
            pytest.fail('Analyst cannot query the Repository')
    class Tools:
        repository=ForbiddenRepository()
    state=P1State(run_id='analyst-boundary',request=request(),
        evidence=[Evidence.model_validate(e) for e in result.data])
    flow=P1Workflow(DemoResearchAdapter(),Tools(),Settings(),'COIN',monotonic()+5,{})
    result=asyncio.run(flow.analyst(state))
    assert len(result['draft_claims'])==5 and not result['errors']
