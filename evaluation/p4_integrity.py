"""Verify prior sealed audits/approval, stage-authorized deltas and regression history."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
ALLOWED={'README.md','api/main.py','app/service.py','frontend/app.py','evaluation/run_regression.py','evaluation/regression/cases.jsonl','tests/test_p3.py'}


def main():
    seals={}
    for seal in sorted((ROOT/'evaluation/audit').glob('P[0123]/attempt-*/SHA256SUMS')):
        count=0
        for line in seal.read_text().splitlines():
            h,path=line.split('  ',1);assert hashlib.sha256((seal.parent/path).read_bytes()).hexdigest()==h,path
            count+=1
        seals[str(seal.parent.relative_to(ROOT))]=count
    previous=ROOT/'evaluation/audit/P3/attempt-001'
    base=json.loads((previous/'artifacts/worktree_manifest.json').read_text())
    changes=[]
    for entry in base:
        if hashlib.sha256((ROOT/entry['path']).read_bytes()).hexdigest()!=entry['sha256']:
            assert entry['path'] in ALLOWED,entry['path'];changes.append(entry['path'])
    assert (ROOT/'evaluation/regression/cases.jsonl').read_bytes().startswith((previous/'artifacts/source/evaluation/regression/cases.jsonl').read_bytes())
    approval=json.loads((ROOT/'evaluation/audit/approvals/H3-20261010T170940835520.json').read_text())
    assert approval['audit_sha256']==hashlib.sha256((previous/'P3_audit.json').read_bytes()).hexdigest()
    assert approval['gate_status']=='APPROVED' and approval['real_provider_semantic_status']=='BLOCKED'
    print(json.dumps({'status':'PASS','prior_seals':seals,'authorized_P4_extensions':changes,
        'H3_recorded':True,'prior_regression_records_preserved':True,'core_P0_P1_P2_unchanged':True}))


if __name__=='__main__':main()
