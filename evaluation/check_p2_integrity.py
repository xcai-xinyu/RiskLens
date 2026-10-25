"""Read-only history, protected-contract and lock verification; never reads .env."""
import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    verified=[]
    for seal in sorted((ROOT/'evaluation/audit').glob('P[01]/*/SHA256SUMS')):
        for line in seal.read_text().splitlines():
            expected,name=line.split('  ',1)
            assert sha(seal.parent/name)==expected, str(seal.parent/name)
        verified.append(str(seal.relative_to(ROOT)))
    approval=json.loads((ROOT/'evaluation/audit/approvals/H1-20261008T160425634504.json').read_text())
    assert approval['approval_text']=='APPROVE H1' and approval['gate_status']=='APPROVED'
    attempt=ROOT/approval['reviewed_attempt']
    assert sha(attempt/'P1_audit.json')==approval['audit_sha256']
    assert sha(attempt/'SHA256SUMS')==approval['seal_sha256']
    manifest=json.loads((attempt/'artifacts/worktree_manifest.json').read_text())
    extension={'README.md','app/p1_service.py','app/p1_runtime.py','app/p1_worker.py','app/service.py',
               'main.py','evaluation/run_regression.py','evaluation/regression/cases.jsonl'}
    preserved=[]
    for item in manifest:
        if item['path'] not in extension:
            assert sha(ROOT/item['path'])==item['sha256'], item['path']
            preserved.append(item['path'])
    locks=[]
    for line in (ROOT/'requirements.txt').read_text().splitlines():
        match=re.match(r'^([a-zA-Z0-9_-]+)==([^ ;]+)$',line)
        if match:
            assert version(match[1])==match[2], match[1]
            locks.append(match[1])
    print(json.dumps({'history_seals_verified':verified,'h1_approval':'APPROVED',
        'preserved_p1_manifest_files':preserved,'p2_extensions':sorted(extension),
        'installed_locks_verified':len(locks)},indent=2))


if __name__=='__main__':
    main()
