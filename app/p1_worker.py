"""Internal WSL worker. JSON stdin contains data only, never executable instructions."""
import asyncio
import json
import subprocess
import sys

from app.config import Settings
from app.p1_service import research


def main():
    try:
        raw=sys.stdin.buffer.read(100_001)
        if len(raw)>100_000:
            raise ValueError('corpus_configuration_error')
        payload=json.loads(raw)
        # Read Git provenance before native Chroma clients/threads are initialized.
        commit=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True,
            check=False,timeout=3).stdout.strip() or None
        dirty=bool(subprocess.run(['git','status','--porcelain'],capture_output=True,text=True,
            check=False,timeout=3).stdout)
        state=asyncio.run(research(payload['request'],Settings.model_validate(payload['settings']),
            corpus=payload['corpus'],directory=payload['directory'],stage=payload.get('stage','P1'),provenance={'git_commit':commit,'dirty':dirty}))
        print(state.model_dump_json())
        return 0
    except ValueError as exc:
        code='company_unresolved' if str(exc).startswith('company_unresolved') else 'corpus_configuration_error'
    except TimeoutError:
        code='run_timeout'
    except Exception:  # noqa: BLE001 - redact every worker boundary exception
        code='p1_worker_error'
    print(json.dumps({'error_code':code}))
    return 2


if __name__=='__main__':
    raise SystemExit(main())
