"""Append-only command capture and final immutable P2 audit assembly."""
import json
import os
import subprocess
import sys
from datetime import datetime
from importlib.metadata import distributions
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
ATTEMPT=ROOT/'evaluation/audit/P2'/os.environ.get('P2_AUDIT_ATTEMPT','attempt-001')
if ATTEMPT.name not in ('attempt-001','attempt-002'):
    raise ValueError('unsupported_attempt')


def timestamp():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def execute(name,argv,expected=0):
    if (ATTEMPT/'SHA256SUMS').exists():
        raise ValueError('audit_sealed')
    artifact=ATTEMPT/'artifacts'/f'{name}.txt'
    if artifact.exists():
        raise ValueError('command_artifact_exists')
    started=timestamp()
    try:
        result=subprocess.run(argv,cwd=ROOT,capture_output=True,text=True,timeout=240,check=False)
        output=result.stdout+'\n'+result.stderr
        code=result.returncode
    except subprocess.TimeoutExpired as exc:
        output=(exc.stdout or b'').decode() if isinstance(exc.stdout,bytes) else (exc.stdout or '')
        output+='\nAudit runner timeout (240s).'
        code=124
    artifact.write_text(output)
    record={'name':name,'argv':argv,'cwd':str(ROOT),'started_at':started,
            'ended_at':timestamp(),'exit_code':code,'expected_exit_code':expected,
            'artifact':str(artifact.relative_to(ATTEMPT))}
    with (ATTEMPT/'commands.jsonl').open('a') as stream:
        stream.write(json.dumps(record)+'\n')
    print(json.dumps(record),flush=True)
    return code


if __name__=='__main__':
    ATTEMPT.joinpath('artifacts').mkdir(parents=True,exist_ok=True)
    if sys.argv[1]=='packages':
        p=ATTEMPT/'artifacts/installed_packages.json'
        with p.open('x') as stream:
            json.dump(sorted([(d.metadata['Name'],d.version) for d in distributions()]),stream,indent=2)
    else:
        start=2
        expected=0
        if sys.argv[2].startswith('--expected-exit='):
            expected=int(sys.argv[2].split('=',1)[1])
            start=3
        raise SystemExit(execute(sys.argv[1],sys.argv[start:],expected))
