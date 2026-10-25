"""P4 append-only command evidence, including failures; never alter previous stages."""
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evaluation/audit/P4/attempt-001'


def main():
    name,*command=sys.argv[1:]
    (OUT/'artifacts').mkdir(parents=True,exist_ok=True)
    path=OUT/'artifacts'/(name+'.txt');started=datetime.now(UTC).isoformat()
    with path.open('x') as stream:
        try:
            result=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,check=False,timeout=360)
            code=result.returncode
        except subprocess.TimeoutExpired:
            code=124;stream.write('AUDIT COMMAND TIMEOUT\n')
    record={'command':command,'cwd':str(ROOT),'started_at':started,'ended_at':datetime.now(UTC).isoformat(),'exit_code':code,'output':str(path.relative_to(ROOT))}
    with (OUT/'commands.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n')
    print(json.dumps(record));print(path.read_text());return code


if __name__=='__main__':raise SystemExit(main())
