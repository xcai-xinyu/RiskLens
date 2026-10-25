"""Actual localhost HTTP, Streamlit AppTest HTTP submission, and process restart smoke.
Model responses remain simulated; numeric history fixtures are opt-in synthetic seeds.
"""
import argparse
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from evaluation.p3_review_seed import seed


def wait_health(client,path,process,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise AssertionError('Local server exited before health check')
        try:
            response=client.get(path)
            if response.status_code==200:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(.2)
    raise AssertionError('Local server startup timeout')


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)


def main(out,database):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    seed_ids=seed(database)
    env=os.environ.copy();env['RISKLENS_DB_PATH']=str(Path(database).resolve())
    # Only explicit harmless configuration names are injected; secrets are not inspected/logged.
    env['RISKLENS_API_URL']='http://127.0.0.1:8765'
    os.environ['RISKLENS_API_URL']=env['RISKLENS_API_URL']
    api_log=(out/'api-server.txt').open('x');ui_log=(out/'streamlit-server.txt').open('x')
    api=None;ui=None
    try:
        def start_api():
            return subprocess.Popen([sys.executable,'-m','uvicorn','api.main:app','--host','127.0.0.1','--port','8765'],cwd=ROOT,env=env,stdout=api_log,stderr=subprocess.STDOUT)
        api=start_api()
        with httpx.Client(base_url=env['RISKLENS_API_URL'],timeout=5,trust_env=False) as client:
            health=wait_health(client,'/health',api).json()
            from streamlit.testing.v1 import AppTest
            # Warm Python's inspect source/module cache before widget deadline on WSL.
            inspect.stack()
            app=AppTest.from_file(str(ROOT/'frontend/app.py'),default_timeout=60).run()
            assert not app.exception
            app.button[0].click().run();assert not app.exception
            run_id=app.session_state['active_run']
            deadline=time.monotonic()+190
            while True:
                run=client.get('/runs/'+run_id).json()
                if run['status'] not in ('queued','running'):
                    break
                assert time.monotonic()<deadline
                time.sleep(.25)
            assert run['status']=='partial' and run['report_available']
            app.run();assert not app.exception
            citation_labels=[e.label for e in app.expander if e.label.startswith('Citation ·')]
            calculation_labels=[e.label for e in app.expander if e.label.startswith('Calculation ·')]
            assert citation_labels and calculation_labels
            report=client.get('/reports/'+run_id).json()
            assert report['metadata']['mode']=='demo' and report['metadata']['simulated']
            assert len(report['claims'])==5
            snapshot=client.get(f'/reports/{run_id}/export?format=json').content
            (out/'http-report.json').write_bytes(snapshot)
            (out/'http-report.md').write_bytes(client.get(f'/reports/{run_id}/export?format=markdown').content)
            comparison=client.post('/reports/compare',json={'run_ids':seed_ids}).json()
            assert not comparison['directly_comparable'] and comparison['risk_change_conclusion'] is None
            (out/'http-comparison.json').write_text(json.dumps(comparison,indent=2))
            app.radio[0].set_value('Report History').run()
            app.selectbox[0].set_value(seed_ids[0]).run();assert not app.exception
            numeric_text=[e.value for e in app.text if 'FY:2024' in e.value and 'USD' in e.value]
            assert numeric_text
            app.multiselect[0].set_value(seed_ids).run()
            next(b for b in app.button if b.label=='Compare reports').click().run()
            assert not app.exception and any('Not directly comparable' in e.value for e in app.warning)
            stop(api);api=start_api();wait_health(client,'/health',api)
            after=client.get(f'/reports/{run_id}/export?format=json').content
            assert after==snapshot
            history=client.get('/reports').json();assert history['total']>=3
            # Start the real Streamlit HTTP server too; no browser screenshots are fabricated.
            ui=subprocess.Popen([sys.executable,'-m','streamlit','run','frontend/app.py','--server.port','8766'],cwd=ROOT,env=env,stdout=ui_log,stderr=subprocess.STDOUT)
            with httpx.Client(base_url='http://127.0.0.1:8766',timeout=5,trust_env=False) as ui_client:
                assert wait_health(ui_client,'/_stcore/health',ui).text=='ok'
                assert ui_client.get('/').status_code==200
            evidence={'status':'EXECUTED','http_health':health,'run_id':run_id,'run_status':run['status'],
                'report_status':report['status'],'model_semantics':'SIMULATED; real fixed PDF sources',
                'numeric_history':'SYNTHETIC P2 attempt-002 snapshots, explicitly seeded',
                'app_test_transport':'actual localhost HTTP API; no API monkeypatch',
                'citation_expanders':citation_labels,'metric_expanders':calculation_labels,
                'numeric_inputs_rendered':numeric_text,'history_compare_warning_rendered':True,
                'api_process_restart':True,'report_bytes_unchanged':True,
                'report_sha256':hashlib.sha256(snapshot).hexdigest(),'history_total':history['total'],
                'streamlit_http_health':'ok','browser_visual_review':'NOT VERIFIED; reserved for H3 human review',
                'provider_semantic_smoke':'BLOCKED; no live-provider validation performed'}
            (out/'runtime-evidence.json').write_text(json.dumps(evidence,indent=2))
            print(json.dumps(evidence))
    finally:
        if ui:stop(ui)
        if api:stop(api)
        api_log.close();ui_log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True);parser.add_argument('--database',required=True)
    args=parser.parse_args();main(args.output,args.database)
