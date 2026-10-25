"""Append-only P3 command recorder; outputs preserve failures and subsequent corrections."""
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
ATTEMPT=ROOT/'evaluation/audit/P3/attempt-001'


def main():
    name,*command=sys.argv[1:]
    artifacts=ATTEMPT/'artifacts';artifacts.mkdir(parents=True,exist_ok=True)
    output=artifacts/(name+'.txt')
    if output.exists():
        raise SystemExit('immutable command output already exists: '+name)
    start=datetime.now(UTC).isoformat()
    with output.open('x') as stream:
        try:
            result=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,
                                  check=False,timeout=300)
            code=result.returncode
        except subprocess.TimeoutExpired:
            code=124;stream.write('\nAUDIT RUNNER TIMEOUT (300s)\n')
    record={'command':command,'cwd':str(ROOT),'started_at':start,
        'ended_at':datetime.now(UTC).isoformat(),'exit_code':code,'output':str(output.relative_to(ROOT))}
    with (ATTEMPT/'commands.jsonl').open('a') as stream:
        stream.write(json.dumps(record)+'\n')
    print(json.dumps(record));print(output.read_text())
    return code


if __name__=='__main__':
    raise SystemExit(main())
