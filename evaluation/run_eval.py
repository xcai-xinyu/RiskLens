"""Explicit demo or separately configured offline provider evaluation; no fallback."""
import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.environment import load_environment
from evaluation.repository import EvaluationRepository
from evaluation.runner import evaluate


def main():
    load_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['demo','offline_research'],default='demo')
    parser.add_argument('--dataset',default=str(ROOT/'evaluation/datasets/p4-v2'))
    parser.add_argument('--output',default=str(ROOT/'evaluation/results'))
    parser.add_argument('--total-timeout',type=float,default=300,help='Total experiment deadline, max 3600 seconds')
    parser.add_argument('--development-case', help='Run exactly one frozen development case; smoke only')
    parser.add_argument('--fault-case',default=None,help='Explicit isolated engineering fault probe; recorded separately')
    parser.add_argument('--internal-worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 0 < args.total_timeout <= 3600:
        parser.error('total-timeout must be >0 and <=3600')
    if not args.internal_worker:
        from evaluation.p4_process import launch
        code=launch(args.total_timeout+2)
        if code==124:
            print(json.dumps({'status':'failed','code':'evaluation_hard_timeout','worker_group_terminated':True}))
        return code
    async def bounded():
        return await asyncio.wait_for(evaluate(args.dataset,mode=args.mode,fault_case=args.fault_case,development_case=args.development_case),timeout=args.total_timeout)
    try:
        result=asyncio.run(bounded())
    except (ValueError,AssertionError,TimeoutError):
        print(json.dumps({'status':'BLOCKED_OR_FAILED','code':'evaluation_configuration_or_integrity',
            'message':'Check frozen hashes and explicit provider configuration; no demo fallback.'}));return 2
    identifier='p4-'+datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')
    EvaluationRepository(args.output).save(identifier,result)
    print(json.dumps({'evaluation_id':identifier,'mode':args.mode,'simulated':result['simulated'],
        'cases':len(result['shared_snapshots']),'arm_records':len(result['records']),
        'real_model_quality_status':result['real_model_quality_status'],'output':str(Path(args.output)/identifier)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
