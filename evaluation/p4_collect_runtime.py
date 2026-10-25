"""Collect two actual immutable reruns; differences in timing are not quality gains."""
import json
import shutil
from pathlib import Path

from evaluation.repository import EvaluationRepository

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evaluation/audit/P4/attempt-001/artifacts'


def main():
    runs=[]
    for name,directory in [('run1','/tmp/risklens-p4-final-1'),('run2','/tmp/risklens-p4-final-2'),('failure_probe','/tmp/risklens-p4-failure-probe')]:
        repo=EvaluationRepository(directory);identifier=repo.history()[-1]['evaluation_id'];data=repo.get(identifier)
        shutil.copytree(Path(directory)/identifier,OUT/name)
        runs.append(data)
    a,b,probe=runs
    assert a['protocol_hash']==b['protocol_hash']
    assert a['execution']['engine_hashes']==b['execution']['engine_hashes']
    for r,s in zip(a['records'],b['records'],strict=True):
        assert (r['case_id'],r['arm'])==(s['case_id'],s['arm'])
        assert r['shared_snapshot_hash']==s['shared_snapshot_hash']
        assert r['output']==s['output'] and r['score']==s['score']
    assert probe['aggregate']['A']['cases']==30 and probe['aggregate']['A']['failed']==1
    comparison={'status':'EXECUTED','protocol_hash_equal':True,'engine_hashes_equal':True,
        'paired_outputs_and_scores_equal':True,'records_per_run':150,'cases_per_run':30,
        'timing_run1':{arm:v['latency_seconds'] for arm,v in a['aggregate'].items()},
        'timing_run2':{arm:v['latency_seconds'] for arm,v in b['aggregate'].items()},
        'latency_exact_match_required':False,'cost':None,'real_provider_status':'BLOCKED',
        'quality_improvement_claim':None,'failure_probe_failed_cases':1,'failure_probe_scope':'Separate explicit fault injection; not mixed into primary results'}
    with (OUT/'rerun_comparison.json').open('x') as f:json.dump(comparison,f,indent=2)
    destination=ROOT/'evaluation/results'/b['evaluation_id']
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(Path('/tmp/risklens-p4-final-2')/b['evaluation_id'],destination)
    rows=['# DEMO / SYNTHETIC engineering measurements — candidate annotations, not model-quality proof','',
          '| Arm | citation correctness n/d | unsupported n/d | completeness n/d | numerical accuracy n/d | failed |',
          '| --- | --- | --- | --- | --- | --- |']
    for arm,item in b['aggregate'].items():
        values=[]
        for metric in ('citation_correctness','unsupported_rate','completeness','numerical_accuracy'):
            v=item['metrics'][metric];values.append(f"{v['numerator']}/{v['denominator']}" if v['denominator'] else 'N/A')
        rows.append('| '+arm+' | '+' | '.join(values)+f" | {item['failed']} |")
    rows+=['','Numbers describe deliberately constructed fixture behavior only. Repair is separate. Read completeness and citation denominators together; filtering changes coverage. Usage/cost null; real-company heldout and provider validation BLOCKED.']
    with (OUT/'aggregate_table.md').open('x') as f:f.write('\n'.join(rows)+'\n')
    print(json.dumps(comparison))


if __name__=='__main__':main()
