"""Keyless HTTP demo and unchanged persisted-snapshot check; no provider client."""
import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check(base_url, state_path, *, verify=False, wait_seconds=240):
    if urlsplit(base_url).hostname not in ('localhost', '127.0.0.1', 'api'):
        raise ValueError('local_demo_endpoint_required')
    state_path = Path(state_path)

    def get(path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base_url+path, data=data, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)

    assert get('/health')[1]['status'] == 'ok'
    if verify:
        old = json.loads(state_path.read_text())
        run_id = old['run_id']
    else:
        if state_path.exists():
            raise FileExistsError('smoke_state_already_exists')
        status, submitted = get('/research', {'company':'Coinbase',
            'question':'Assess liquidity and regulatory risks with evidence.',
            'as_of_date':'2025-02-13', 'risk_categories':['liquidity','regulatory'],'mode':'demo'})
        assert status == 202
        run_id = submitted['run_id']
        deadline = time.monotonic()+wait_seconds
        while True:
            _, run = get('/runs/'+run_id)
            if run['status'] in ('completed', 'partial'):
                break
            if run['status'] == 'failed' or time.monotonic() >= deadline:
                raise RuntimeError('demo_failed_or_deadline_exceeded')
            time.sleep(1)
    status, report = get('/reports/'+run_id)
    assert status == 200 and report['metadata']['simulated'] is True
    assert report['metadata']['mode'] == 'demo'
    assert report['claims'] and report['sources']
    assert all(c['evidence_ids'] for c in report['claims'])
    assert any(row['run_id']==run_id for row in get('/reports?limit=100')[1]['items'])
    record = {'run_id':run_id,'report_sha256':digest(report),'status':report['status'],
              'simulated':True,'claims':len(report['claims']),'metrics':len(report['metrics'])}
    if verify:
        assert record == old, 'persisted_report_changed'
    else:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with state_path.open('x') as stream:
            json.dump(record, stream, indent=2)
    return dict(record, check='persistence' if verify else 'demo', result='PASS', paid_requests=0)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--api',default='http://127.0.0.1:8000')
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--verify-persisted',action='store_true')
    args=p.parse_args()
    print(json.dumps(check(args.api.rstrip('/'),args.state,verify=args.verify_persisted)))


if __name__=='__main__': main()
