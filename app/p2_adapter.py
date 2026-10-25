"""P2 model capabilities; fixtures are explicit simulated decisions, not truth."""
import json

from app.adapter import ROOT
from app.p1_adapter import DemoResearchAdapter, OpenAIResearchAdapter
from app.p1_contracts import AnalystDraft
from app.p2_contracts import MetricExtraction, RepairBatch, SemanticBatch


class DemoP2Adapter(DemoResearchAdapter):
    model = 'p2-demo-v1'
    document_label = 'REAL FIXED DOCUMENT'

    def __init__(self):
        super().__init__()
        self.verifier_fixture = json.loads((ROOT/'tests/fixtures/p2_semantic_v1.json').read_text())

    async def make_plan(self, request, company_id):
        from app.tools.metrics import FORMULAS
        plan = await super().make_plan(request, company_id)
        # Preserve this explicit demo's existing five missing-input diagnostics.
        # Real empty plans are no longer silently expanded by the workflow.
        if 'liquidity' in request.risk_categories:
            plan.required_metrics = list(FORMULAS)
        return plan

    async def extract(self, request, evidence, required_metrics):
        # Coinbase fixed demo does not bind these five formulas' required inputs.
        # Missing data remains missing; unit tests exercise actual calculations.
        return MetricExtraction(requests=[{'name':name, 'inputs':[]} for name in required_metrics])

    async def verify(self, claims, evidence, metrics):
        decisions = []
        for claim in claims:
            fixed = self.verifier_fixture.get(claim.claim_id)
            if fixed and claim.text == fixed['text']:
                decisions.append({'claim_id':claim.claim_id, **fixed['decision']})
            else:
                decisions.append({'claim_id':claim.claim_id, 'status':'unverifiable',
                    'explanation':'No frozen simulated semantic decision for this exact draft.',
                    'conflicts':[], 'overstatement':False})
        return SemanticBatch(decisions=decisions)

    async def repair(self, claims, decisions, evidence, metrics):
        return RepairBatch(claims=[])


class OpenAIP2Adapter(OpenAIResearchAdapter):
    def messages(self, role, data):
        if role in ('extract', 'verifier', 'repair', 'analyst'):
            return [('system',(ROOT/f'app/prompts/{role}_p2_v1.txt').read_text()),
                    ('human',json.dumps(data,ensure_ascii=False))]
        return super().messages(role, data)

    async def extract(self, request, evidence, required_metrics):
        return await self.structured('extract', {'request':request.model_dump(mode='json'),
            'required_metrics':required_metrics, 'evidence':[e.model_dump(mode='json') for e in evidence]},
            MetricExtraction)

    async def analyze(self, request, evidence, metrics):
        return await self.structured('analyst', {'request':request.model_dump(mode='json'),
            'evidence':[e.model_dump(mode='json') for e in evidence],
            'metrics':[m.model_dump(mode='json') for m in metrics]}, AnalystDraft)

    async def verify(self, claims, evidence, metrics):
        return await self.structured('verifier', {'claims':[c.model_dump(mode='json') for c in claims],
            'evidence':[e.model_dump(mode='json') for e in evidence],
            'metrics':[m.model_dump(mode='json') for m in metrics]}, SemanticBatch)

    async def repair(self, claims, decisions, evidence, metrics):
        return await self.structured('repair', {'claims':[c.model_dump(mode='json') for c in claims],
            'decisions':[d.model_dump(mode='json') for d in decisions],
            'evidence':[e.model_dump(mode='json') for e in evidence],
            'metrics':[m.model_dump(mode='json') for m in metrics]}, RepairBatch)


class GroqP2Adapter(OpenAIP2Adapter):
    provider = 'groq'
