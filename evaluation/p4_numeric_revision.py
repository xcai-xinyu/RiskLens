"""Preserve preliminary v1; add missing numerical coverage as a new frozen v2 before runs."""
import json

from app.schemas import MetricInput
from app.tools.metrics import calculate_metric
from evaluation.p4_dataset import ROOT, sha

DATA=ROOT/'evaluation/datasets/p4-v2'


def main():
    DATA.mkdir(exist_ok=False)
    previous=ROOT/'evaluation/datasets/p4-v1'
    cases=[json.loads(s) for s in (previous/'dataset.jsonl').read_text().splitlines()]
    snapshots=[json.loads(s) for s in (previous/'snapshots.jsonl').read_text().splitlines()]
    annotations=[json.loads(s) for s in (previous/'annotations.jsonl').read_text().splitlines()]
    values=['2','2.1','2.2','2.3','2.4','2.5','2.6','2.7','2.8','2.9','3','3.1','3.2','3.3','3.4','3.5','3.6','3.7','3.8','3.9']
    for case,snapshot,annotation in zip(cases,snapshots,annotations,strict=True):
        if case['split']=='development':continue
        n=int(case['case_id'].split('-')[1]);kind=n%5;eid=snapshot['evidence'][0]['evidence_id']
        inputs=[MetricInput(name=name,value=value,unit='million',currency='USD',period='FY:2024',evidence_id=eid,extraction_status='extracted') for name,value in [('current_assets',str(200+10*n)),('current_liabilities','100')]]
        # Validation fixtures deliberately perturb the input, with independent expectations.
        if kind==1:inputs[1].value=0
        if kind==2:inputs[1].currency='EUR'
        if kind==3:inputs=inputs[:1]
        if kind==4:inputs[1].period='FY:2023'
        actual=calculate_metric('current_ratio',inputs)
        snapshot['metrics']=[actual.model_dump(mode='json')]
        if kind==0:snapshot['baseline'][0]['metric_ids']=[actual.metric_id]
        reference={'name':'current_ratio','formula':'current_assets / current_liabilities',
            'value':values[n] if kind==0 else None,
            'status':'success' if kind==0 else ('missing_input' if kind==3 else 'invalid_input'),
            'inputs':[i.model_dump(mode='json') for i in inputs]}
        case['reference_metrics']=[reference]
        annotation['numeric_basis']='Independent reference: (200+10*n)/100, all million USD FY:2024; literal expected ratio table. Zero, currency, missing and period cases expect null/validation outcome. References do not call Verifier or copy calculated outputs.'
        if kind==4:case['expected_gaps']=['citation_date_invalid']
    for name,items in [('dataset.jsonl',cases),('snapshots.jsonl',snapshots),('annotations.jsonl',annotations)]:
        with (DATA/name).open('x') as f:
            for item in items:f.write(json.dumps(item,ensure_ascii=False)+'\n')
    with (DATA/'model_responses.json').open('x') as f:f.write((previous/'model_responses.json').read_text())
    manifest=json.loads((previous/'manifest.json').read_text())
    manifest.update(version='p4-v2',predecessor='p4-v1',revision_reason='Before any experiment, review found missing numerical evaluation coverage; add 20 independent metric/validation references. Preserve v1; no result-driven gold change.',
        files={**manifest['files'],**{str(p.relative_to(ROOT)):sha(p) for p in DATA.iterdir() if p.is_file()}},
        execution_hashes={str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'app/verification.py',ROOT/'app/p2_workflow.py',ROOT/'app/tools/metrics.py',ROOT/'app/configuration/verification_p2_v1.json',*sorted((ROOT/'app/prompts').glob('*_p2_v1.txt'))]})
    with (DATA/'manifest.json').open('x') as f:json.dump(manifest,f,indent=2)
    print('Frozen p4-v2: 30 candidate-annotated cases; 20 metric validation references; v1 preserved')


if __name__=='__main__':main()
