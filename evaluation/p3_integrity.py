"""Verify prior seals and all P0–P2 worktree files except authorized P3 extensions."""
import hashlib
import importlib.metadata
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
ALLOWED={'app/service.py','main.py','requirements.in','requirements.txt','evaluation/run_regression.py','evaluation/regression/cases.jsonl','README.md'}


def main():
    counts={}
    for seal in sorted((ROOT/'evaluation/audit').glob('P[012]/attempt-*/SHA256SUMS')):
        count=0
        for line in seal.read_text().splitlines():
            digest,name=line.split('  ',1)
            assert hashlib.sha256((seal.parent/name).read_bytes()).hexdigest()==digest,name
            count+=1
        counts[str(seal.parent.relative_to(ROOT))]=count
    baseline=json.loads((ROOT/'evaluation/audit/P2/attempt-002/artifacts/worktree_manifest.json').read_text())
    unchanged=[];extensions=[]
    for entry in baseline:
        path=entry['path']
        digest=hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
        if digest!=entry['sha256']:
            assert path in ALLOWED,'Unauthorized P0–P2 change: '+path
            extensions.append(path)
        else:
            unchanged.append(path)
    baseline_cases=(ROOT/'evaluation/audit/P2/attempt-002/artifacts/source/evaluation/regression/cases.jsonl').read_bytes()
    assert (ROOT/'evaluation/regression/cases.jsonl').read_bytes().startswith(baseline_cases)
    packages=dict(json.loads((ROOT/'evaluation/audit/P2/attempt-002/artifacts/installed_packages.json').read_text()))
    assert all(importlib.metadata.version(name)==version for name,version in packages.items())
    print(json.dumps({'status':'PASS','prior_seals':counts,'unchanged_baseline_files':len(unchanged),
        'authorized_extensions':extensions,'prior_regression_records_preserved':True,'existing_dependency_versions_preserved':True}))


if __name__=='__main__':
    main()
