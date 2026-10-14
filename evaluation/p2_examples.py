"""Reviewable synthetic financial and adversarial runtime; never a live quality eval."""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from app.p1_adapter import DemoResearchAdapter
from app.p1_contracts import AnalystDraft
from app.p2_contracts import MetricExtraction, RepairBatch, SemanticBatch
from app.schemas import Claim, MetricInput, ResearchRequest
from app.service import ResearchService
from app.storage.repository import Repository
from app.tools.metrics import FORMULAS

FIXTURE=ROOT/'tests/fixtures/regression/p2_adversarial_v2.json'


class ExampleAdapter(DemoResearchAdapter):
    model='synthetic-p2-adversarial-v2'
    document_label='SYNTHETIC FIXTURE DOCUMENTS'

    def __init__(self):
        super().__init__()
        self.examples=json.loads(FIXTURE.read_text())

    async def extract(self,request,evidence,required_metrics):
        source=next(e for e in evidence if 'current_assets' in e.quote)
        values={'current_assets':'200','current_liabilities':'100','cash_and_cash_equivalents':'20',
                'total_debt':'80','total_equity':'40','EBIT':'30','interest_expense':'10',
                'OCF_current':'60','OCF_previous':'40'}
        requests=[]
        for name in FORMULAS:
            items=[MetricInput(name=key,value=values[key],unit='million',currency='USD',
                period='FY:2023' if key=='OCF_previous' else 'FY:2024',evidence_id=source.evidence_id,
                extraction_status='extracted') for key in FORMULAS[name][:2]]
            requests.append({'name':name,'inputs':items})
        return MetricExtraction(requests=requests)

    async def analyze(self,request,evidence,metrics):
        source=next(e for e in evidence if 'current_assets' in e.quote)
        return AnalystDraft(claims=[Claim(claim_id=c['claim_id'],text=c['text'],claim_type='fact',
            risk_category='liquidity',evidence_ids=[source.evidence_id],metric_ids=[]) for c in self.examples['claims']])

    async def verify(self,claims,evidence,metrics):
        by_id={c['claim_id']:c for c in self.examples['claims']}
        decisions=[]
        for c in claims:
            template=by_id[c.claim_id]
            revised=c.text==template.get('narrowed')
            decisions.append({'claim_id':c.claim_id,'status':'supported' if revised else template['status'],
                'explanation':'Narrowed amount is directly stated in source.' if revised else template['explanation'],
                'conflicts':template.get('conflicts',[]),'overstatement':template.get('overstatement',False) and not revised})
        return SemanticBatch(decisions=decisions)

    async def repair(self,claims,*args):
        templates={c['claim_id']:c for c in self.examples['claims']}
        return RepairBatch(claims=[c.model_copy(update={'text':templates[c.claim_id]['narrowed']}) for c in claims])


async def main(out):
    out=Path(out)
    out.mkdir(parents=True,exist_ok=True)
    fixture=json.loads(FIXTURE.read_text())
    with tempfile.TemporaryDirectory(prefix='risklens-p2-examples-') as directory:
        base=Path(directory)
        documents=[]
        for n,key in enumerate(('source','conflicting_source')):
            raw=fixture[key].encode()
            path=base/f'source-{n}.txt';path.write_bytes(raw)
            documents.append({'path':path.name,'company_id':'COIN','title':f'SYNTHETIC P2 review source {n}',
                'url':f'https://example.com/p2-synthetic-{n}','published_at':'2025-01-01','source_group_id':f'synthetic-{n}',
                'sha256':hashlib.sha256(raw).hexdigest()})
        manifest={'version':'p2-synthetic-v1','company_aliases':{'coinbase':'COIN'},'documents':documents,
                  'historical_limitation':'Entire corpus is synthetic engineering test data, not Coinbase disclosure.'}
        corpus=base/'corpus.json';corpus.write_text(json.dumps(manifest))
        req=ResearchRequest(company='Coinbase',question='current_assets current_liabilities cash total_debt EBIT OCF liquidity',
            as_of_date='2025-02-13',risk_categories=['liquidity'],mode='demo')
        state=await ResearchService().verify_research(req,corpus=corpus,directory=base/'store',adapter=ExampleAdapter())
        assert not state.errors
        assert state.report.metadata['label']=='SIMULATED MODEL RESPONSES / SYNTHETIC FIXTURE DOCUMENTS'
        assert {m.name:str(m.value) for m in state.metrics}==fixture['expected_metrics']
        assert [c.claim_id for c in state.report.claims]==['c5']
        c1=next(v for v in state.verification_results if v.claim_id=='c1')
        assert c1.status=='unverifiable' and c1.conflicts
        assert not c1.checks['no_overstatement'] and not c1.checks['no_conflicts']
        assert 'liquidity is guaranteed safe' not in state.report.summary
        assert state.repair_count==0
        assert len(state.lineage)==5
        from app.reporting import render_json, render_markdown
        outputs={'synthetic_state.json':state.model_dump_json(indent=2),
                 'synthetic_report.json':render_json(state.report),
                 'synthetic_report.md':render_markdown(state.report),
                 'synthetic_source.txt':fixture['source'],
                 'synthetic_conflicting_source.txt':fixture['conflicting_source']}
        repo=Repository(base/'store')
        trace=[{'evidence':e.model_dump(mode='json'),'trace':repo.trace(e,req.as_of_date)} for e in state.evidence]
        from time import monotonic
        from types import SimpleNamespace

        from app.config import Settings
        from app.p2_workflow import P2Workflow
        probe=state.model_copy(deep=True)
        original=next(c for c in probe.draft_claims if c.claim_id=='c1')
        narrowed=original.model_copy(update={'text':'Cash is 20 million USD FY:2024.'})
        workflow=P2Workflow(ExampleAdapter(),SimpleNamespace(repository=repo),
            Settings.model_validate(state.metadata['config']),'COIN',monotonic()+10,state.metadata)
        results=await workflow.verify_claims(probe,[narrowed])
        assert results[0].status=='unverifiable' and results[0].conflicts
        assert results[0].checks['no_overstatement'] and not results[0].checks['no_conflicts']
        assert not results[0].checks['semantic_support'] and results[0].evidence_support_score is None
        assert probe.budget.llm_calls<=state.metadata['config']['max_llm_calls']
        outputs['c1_narrowing_probe.json']=json.dumps({
            'label':'Independent narrowed-text re-verification probe; no workflow repair was performed',
            'original':original.model_dump(mode='json'),'narrowed':narrowed.model_dump(mode='json'),
            'verification':results[0].model_dump(mode='json'),
            'workflow_repair_count':state.repair_count,
            'model_calls_before':state.budget.llm_calls,'probe_model_calls_after':probe.budget.llm_calls,
            'max_llm_calls':state.metadata['config']['max_llm_calls'],
            'final_disposition':'Excluded: same-period cash conflict persists'},indent=2)
        repo.close()
        outputs['synthetic_trace.json']=json.dumps(trace,indent=2)
        outputs['synthetic_metrics.json']=json.dumps([m.model_dump(mode='json') for m in state.metrics],indent=2)
        for name,text in outputs.items():
            with (out/name).open('x') as f:
                f.write(text)
        # Check source input validation and metric references through the same Verifier.
        repo=Repository(base/'store')
        ratio=next(m for m in state.metrics if m.name=='current_ratio')
        numeric=Claim(claim_id='metric-claim',text='Current ratio is 2 for FY:2024.',claim_type='fact',
            risk_category='liquidity',evidence_ids=[],metric_ids=[ratio.metric_id])
        from app.verification import hard_checks
        checks,reasons=hard_checks(numeric,state.evidence,state.metrics,repo,req.as_of_date)
        repo.close()
        assert all(checks.values()) and not reasons
        print(json.dumps({'status':'EXECUTED','label':fixture['label'],'claims':5,'lineage_records':len(state.lineage),
                          'metrics':5,'repairs':state.repair_count,'budget':state.budget.model_dump()}))


if __name__=='__main__':
    asyncio.run(main(sys.argv[1]))
