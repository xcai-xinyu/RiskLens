"""Frozen paired ablations reuse P2 checks/semantic adapter, never change production Verifier."""
import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from app.config import Settings
from app.p1_contracts import AnalystDraft, CorpusEntry
from app.p2_adapter import OpenAIP2Adapter
from app.p2_contracts import P2State, RepairBatch, SemanticBatch
from app.p2_workflow import P2Workflow
from app.providers import ProviderFailure, classify_error, make_chat_adapter
from app.rag.ingestion import ingest_file
from app.schemas import Claim, Evidence, MetricResult, ResearchRequest
from app.storage.repository import Repository
from app.verification import detect_cash_conflicts, hard_checks
from evaluation.scorers import VERSION, aggregate, score

ROOT=Path(__file__).resolve().parent.parent


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def failure_record(error):
    """Only safe classifications, never SDK payloads or exception messages."""
    if isinstance(error, ProviderFailure):
        return error.record()
    if isinstance(error, ValueError):
        return {'code':'structured_output_invalid','http_status':None,'retryable':False}
    return classify_error(error).record()


def recorded_usage(adapter):
    """Preserve known entries; a cancelled call with no SDK response is unknown."""
    usage=getattr(adapter,'usage',None)
    if usage is None:
        return None
    entries=list(usage)
    return entries+[None]*max(0,getattr(adapter,'calls',len(entries))-len(entries))


def load_dataset(directory):
    directory=Path(directory);manifest=json.loads((directory/'manifest.json').read_text())
    for path,h in {**manifest['files'],**manifest.get('execution_hashes',{})}.items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==h,'Frozen hash mismatch: '+path
    cases=[json.loads(l) for l in (directory/'dataset.jsonl').read_text().splitlines()]
    snapshots={s['id']:s for s in [json.loads(l) for l in (directory/'snapshots.jsonl').read_text().splitlines()]}
    assert len({c['case_id'] for c in cases})==len(cases),'Duplicate case IDs'
    for key in ('company_group','document_group'):
        dev={c[key] for c in cases if c['split']=='development'}
        held={c[key] for c in cases if c['split']=='heldout'}
        assert not dev&held,'Split leakage: '+key
    assert all(c['snapshot_id'] in snapshots for c in cases)
    return manifest,cases,snapshots


class FixtureAdapter:
    provider='fixture';model='p4-fixture-v1';usage=None
    def __init__(self,response):
        self.response=response;self.calls=0
    async def raw(self,request):
        # Explicit frozen raw-model simulation: no evidence supplied, abstention.
        self.calls+=1;return AnalystDraft(claims=[])
    async def verify(self,claims,evidence,metrics):
        self.calls+=1
        return SemanticBatch(decisions=[{'claim_id':c.claim_id,
            'status':'supported' if c.text==self.response.get('narrowed') else self.response['status'],
            'explanation':'SIMULATED frozen P4 response, not an independent truth judgment.',
            'overstatement':self.response['overstatement'] and c.text!=self.response.get('narrowed'),
            'conflicts':[]} for c in claims])
    async def repair(self,claims,*args):
        self.calls+=1
        narrowed=self.response.get('narrowed')
        return RepairBatch(claims=[c.model_copy(update={'text':narrowed}) for c in claims] if narrowed else [])


class ProviderAdapter(OpenAIP2Adapter):
    def __init__(self, call_budget=None):
        selected = make_chat_adapter(stage='P2')
        self.__dict__.update(selected.__dict__)
        self.provider = selected.provider
        self.call_budget = call_budget

    async def invoke(self, runnable, messages):
        if self.call_budget is not None:
            if self.call_budget['calls'] >= self.call_budget['limit']:
                failure=ProviderFailure('llm_budget_exhausted')
                self.failures.append(failure.record())
                raise failure
            self.call_budget['calls'] += 1
        return await super().invoke(runnable,messages)

    async def raw(self,request):
        from evaluation.prompts import raw_messages
        options={'strict':True} if self.provider=='groq' else {}
        result=await self.invoke(self.client.with_structured_output(AnalystDraft,method='json_schema',include_raw=True,**options),raw_messages(request))
        raw=result.get('raw');self.usage.append(raw.usage_metadata if raw else None)
        if result.get('parsing_error') or result.get('parsed') is None:
            raise ValueError('raw_contract_error')
        return AnalystDraft.model_validate(result['parsed'])


async def evaluate(dataset,*,mode='demo',fault_case=None,adapter_factory=None,development_case=None):
    manifest,cases,snapshots=load_dataset(dataset)
    selected_cases = cases
    if development_case is not None:
        selected_cases = [c for c in cases if c['case_id'] == development_case and c['split'] == 'development']
        if len(selected_cases) != 1:
            raise ValueError('development_case_missing_or_not_development')
    engine_paths=[ROOT/'evaluation/runner.py',ROOT/'evaluation/scorers.py',ROOT/'evaluation/prompts/raw_p4_v1.txt',ROOT/'app/p2_adapter.py',ROOT/'app/p1_adapter.py',ROOT/'app/providers.py',ROOT/'app/environment.py']
    engine_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in engine_paths}
    simulated=mode=='demo' or adapter_factory is not None
    responses=json.loads((Path(dataset)/'model_responses.json').read_text())
    settings=Settings(**manifest['budget']);records=[];shared=[];case_budgets=[]
    for case in selected_cases:
        snapshot=snapshots[case['snapshot_id']]
        request=ResearchRequest.model_validate(dict(snapshot['request'],mode=mode))
        start=monotonic();deadline=start+settings.run_timeout_seconds
        call_budget={'calls':0,'limit':6} if development_case else None
        with tempfile.TemporaryDirectory(prefix='risklens-p4-') as directory:
            repo=Repository(directory)
            try:
                corpus_path=ROOT/snapshot['corpus'];corpus=json.loads(corpus_path.read_text())
                for entry in corpus['documents']:
                    result=ingest_file(repo,corpus_path.parent,CorpusEntry.model_validate(entry))
                    if result.status!='success':raise ValueError('frozen_source_ingestion_failed')
                evidence=[Evidence.model_validate(e) for e in snapshot['evidence']]
                metrics=[MetricResult.model_validate(m) for m in snapshot['metrics']]
                factory=adapter_factory or (lambda response=responses[case['case_id']], budget=call_budget:FixtureAdapter(response) if mode=='demo' else ProviderAdapter(budget))
                # One baseline generation, then a byte-identical paired snapshot for B/C/D.
                baseline_adapter=factory();baseline_calls=0;baseline_usage=None;baseline_start=monotonic()
                if mode=='demo':
                    baseline=[Claim.model_validate(c) for c in snapshot['baseline']]
                    baseline_kind='FROZEN SIMULATED ANALYST REPLAY; original calls not recharged'
                else:
                    baseline_calls=1
                    try:
                        baseline=AnalystDraft.model_validate(await asyncio.wait_for(baseline_adapter.analyze(request,evidence,metrics),timeout=max(.001,deadline-monotonic()))).claims
                        if len({c.claim_id for c in baseline})!=len(baseline) or any(c.risk_category not in request.risk_categories for c in baseline):
                            raise ValueError('baseline_contract_error')
                    except Exception as exc:  # noqa: BLE001 - retain all arms when baseline generation fails
                        baseline_error=failure_record(exc)
                        baseline_usage=recorded_usage(baseline_adapter)
                        failed_snapshot={'request':request.model_dump(mode='json'),
                            'evidence':[e.model_dump(mode='json') for e in evidence],
                            'metrics':[m.model_dump(mode='json') for m in metrics],'draft':None}
                        failed_hash=digest(failed_snapshot)
                        for arm in ('B','C','D','C_REPAIR'):
                            failed={'status':'failed','claims':[],'metrics':[],'gaps':['shared_baseline_failed']}
                            records.append({'case_id':case['case_id'],'split':case['split'],'source_kind':case['source_kind'],
                                'arm':arm,'status':'failed','mode':mode,'simulated':simulated,'model':baseline_adapter.model,'provider':baseline_adapter.provider,'provider_failures':getattr(baseline_adapter,'failures',[]),
                                'output':failed,'score':score(case,failed,set()),'original_score':None,
                                'shared_snapshot_hash':failed_hash,'execution_error':baseline_error,
                                'latency_seconds':monotonic()-start,'llm_calls':1 if arm=='B' else 0,
                                'actual_usage':baseline_usage if arm=='B' else [],
                                'usage_attribution':'shared Analyst charged only to B; dependent arms made no calls',
                                'cost':None,'repair_count':0,'repair_budget':0})
                        raw_adapter=factory()
                        raw_error=None
                        try:
                            raw=AnalystDraft.model_validate(await asyncio.wait_for(raw_adapter.raw(request),timeout=max(.001,deadline-monotonic())))
                            raw_output={'status':'completed','claims':[c.model_dump(mode='json') for c in raw.claims],'metrics':[],'gaps':[]}
                        except Exception as exc:  # noqa: BLE001 - raw arm remains independent of B baseline failure
                            raw_error=failure_record(exc)
                            raw_output={'status':'failed','claims':[],'metrics':[],'gaps':['raw_arm_failed']}
                        records.append({'case_id':case['case_id'],'split':case['split'],'source_kind':case['source_kind'],
                            'arm':'A','status':raw_output['status'],'mode':mode,'simulated':simulated,'model':raw_adapter.model,'provider':raw_adapter.provider,'provider_failures':getattr(raw_adapter,'failures',[]),
                            'output':raw_output,'score':score(case,raw_output,set()),'original_score':None,
                            'shared_snapshot_hash':None,'execution_error':raw_error,'latency_seconds':monotonic()-start,'llm_calls':1,
                            'actual_usage':recorded_usage(raw_adapter),'cost':None,'repair_count':0,'repair_budget':0})
                        shared.append({'case_id':case['case_id'],'status':'failed','hash':failed_hash,
                            'snapshot':failed_snapshot,'baseline_calls':1,'baseline_usage':baseline_usage,
                            'execution_error':baseline_error,'draft_status':'unavailable; Analyst validation failed'})
                        continue
                    baseline_usage=recorded_usage(baseline_adapter)
                    baseline_kind='REAL MODEL ANALYST ON FROZEN RESEARCH SNAPSHOT'
                baseline_latency=monotonic()-baseline_start
                frozen={'request':request.model_dump(mode='json'),'evidence':[e.model_dump(mode='json') for e in evidence],
                    'metrics':[m.model_dump(mode='json') for m in metrics],'draft':[c.model_dump(mode='json') for c in baseline]}
                fingerprint=digest(frozen);shared.append({'case_id':case['case_id'],'hash':fingerprint,'snapshot':frozen,
                    'baseline_kind':baseline_kind,'baseline_calls':baseline_calls,'baseline_usage':baseline_usage,
                    'research_provenance':snapshot['provenance'],'baseline_latency_seconds':baseline_latency})
                for arm in ('A','B','C','D','C_REPAIR'):
                    adapter=FixtureAdapter({}) if arm=='A' and mode=='demo' and adapter_factory is None else factory();arm_start=monotonic();decisions=[];gaps=[];lineage=[];repairs=0;calls=0
                    execution_error=None
                    try:
                        if case['case_id']==fault_case and arm=='A':raise TimeoutError('explicit_engineering_fault')
                        if arm=='A':
                            calls=1
                            result=await asyncio.wait_for(adapter.raw(request),timeout=max(.001,deadline-monotonic()))
                            final=AnalystDraft.model_validate(result).claims;arm_metrics=[];calls=1
                        elif arm=='B':final=baseline;arm_metrics=metrics;calls=baseline_calls
                        elif arm=='D':
                            final=[];arm_metrics=metrics
                            for c in baseline:
                                checks,reasons=hard_checks(c,evidence,metrics,repo,request.as_of_date)
                                conflicts=detect_cash_conflicts(c,evidence,repo,request.as_of_date)
                                passed=all(checks.values()) and not conflicts
                                decisions.append({'claim_id':c.claim_id,'hard_pass':passed,'checks':checks,'conflicts':conflicts,'semantic_checked':False})
                                if passed:final.append(c)
                                else:gaps.extend(reasons);gaps.extend(['source_conflict'] if conflicts else [])
                        else:
                            state=P2State(run_id=case['case_id']+'-'+arm,request=request,documents=repo.documents(),
                                evidence=evidence,metrics=metrics,draft_claims=[c.model_copy(deep=True) for c in baseline])
                            workflow=P2Workflow(adapter,SimpleNamespace(repository=repo),settings,
                                corpus['company_aliases'][request.company.lower()],deadline,{})
                            state.verification_results=await workflow.verify_claims(state,state.draft_claims)
                            if arm=='C_REPAIR' and workflow.after_verifier(state)=='repair':
                                await workflow.repair(state)
                            decisions=[v.model_dump(mode='json') for v in state.verification_results]
                            supported={v.claim_id for v in state.verification_results if v.status=='supported'}
                            final=[c for c in state.draft_claims if c.claim_id in supported]
                            arm_metrics=metrics;calls=state.budget.llm_calls;repairs=state.repair_count
                            lineage=[l.model_dump(mode='json') for l in state.lineage]
                            gaps=[g.code for g in state.gaps]
                            for c in baseline:
                                _,reasons=hard_checks(c,evidence,metrics,repo,request.as_of_date);gaps.extend(reasons)
                        output={'status':'partial' if {'semantic_unavailable','run_timeout','repair_rejected'}&set(gaps) else 'completed','claims':[c.model_dump(mode='json') for c in final],
                                'metrics':[m.model_dump(mode='json') for m in arm_metrics],'gaps':sorted(set(gaps))}
                    except Exception as exc:  # noqa: BLE001 - redact model/network errors and retain failed arms
                        execution_error=failure_record(exc)
                        output={'status':'failed','claims':[],'metrics':[],'gaps':['arm_execution_failed']}
                    if mode!='demo' and arm!='B':
                        calls=getattr(adapter,'calls',calls)
                    eligible_ids={e.evidence_id for e in evidence if repo.document(e.document_id).published_at and repo.document(e.document_id).published_at<=request.as_of_date}
                    records.append({'case_id':case['case_id'],'split':case['split'],'source_kind':case['source_kind'],
                        'arm':arm,'status':output['status'],'mode':mode,'simulated':simulated,
                        'model':adapter.model,'provider':adapter.provider,'provider_failures':getattr(adapter,'failures',[]),'execution_error':execution_error,'scorer_version':VERSION,'shared_snapshot_hash':None if arm=='A' else fingerprint,
                        'output':output,'original_draft':frozen['draft'] if arm!='A' else output['claims'],
                        'score':score(case,output,eligible_ids),
                        'original_score':score(case,dict(output,claims=frozen['draft']),eligible_ids) if arm!='A' and output['status']!='failed' else None,
                        'decisions':decisions,'lineage':lineage,'repair_count':repairs,'repair_budget':1 if arm=='C_REPAIR' else 0,
                        'llm_calls':calls,'llm_call_limit':settings.max_llm_calls,
                        'latency_seconds':baseline_latency if arm=='B' else monotonic()-arm_start,
                        'latency_kind':'shared Analyst generation' if arm=='B' else 'arm incremental execution',
                        'baseline_llm_calls':0 if arm=='A' else baseline_calls,
                        'actual_usage':None if mode=='demo' else (baseline_usage if arm=='B' else recorded_usage(adapter)),'cost':None})
            finally:
                case_budgets.append({'case_id':case['case_id'],'elapsed_seconds':monotonic()-start,
                    'case_timeout_seconds':settings.run_timeout_seconds,
                    'actual_llm_calls':sum(r['llm_calls'] for r in records if r['case_id']==case['case_id']),
                    'shared_call_budget':call_budget if not simulated else None})
                repo.close()
    assert engine_hashes=={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in engine_paths},'Evaluation engine changed during run'
    execution={'timestamp':datetime.now(UTC).isoformat(),'engine_hashes':engine_hashes,
        'provider_versions':sorted({r.get('provider','fixture') for r in records}),'model_versions':sorted({r['model'] for r in records}),'prompt_versions':{'A':'raw-p4-v1','B/C/D':'p2-v1'},
        'rubric_version':'verification-p2-v1','budget':settings.model_dump(),'randomness':None,
        'randomness_reason':'Fixture responses fixed; real provider default sampling not assumed deterministic.',
        'retrieval':'Frozen prior Research snapshot; no new embeddings/search charged to these ablations'}
    return {'selection':{'scope':'development_smoke' if development_case else 'full_dataset', 'case_ids':[c['case_id'] for c in selected_cases]},'case_budgets':case_budgets,'execution':execution,'protocol':manifest,'protocol_hash':digest(manifest),'mode':mode,'simulated':simulated,
        'label':'DEMO / SYNTHETIC ENGINEERING EVALUATION' if simulated else 'REAL MODEL / FROZEN OFFLINE RESEARCH',
        'annotation_status':manifest['annotation_status'],'human_approved':False,
        'real_model_quality_status':'BLOCKED' if simulated or all(r['status']=='failed' for r in records) else 'PENDING_H4',
        'records':records,'shared_snapshots':shared,'aggregate':aggregate(records),
        'split_aggregate':{split:aggregate([r for r in records if r['split']==split]) for split in ('development','heldout')},
        'fault_case':fault_case,'quality_improvement_claim':None,
        'limitations':manifest['limitations']+['Exact-reference scorer cannot judge novel paraphrases; unresolved claims require independent human adjudication.',
            'Prior research snapshot ingestion is frozen; retrieval/embedding latency and cost are not re-measured as model performance.',
            'Real-provider A/B/C/D, independently human-approved gold and real-company heldout evidence remain required for a research-quality conclusion.']}
