"""P4 localhost UI/API reads executed immutable evaluation; no model calls."""
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


def main(results,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env['RISKLENS_EVAL_DIR']=str(Path(results).resolve())
    env['RISKLENS_DB_PATH']=str(output/'runtime.sqlite');env['RISKLENS_API_URL']='http://127.0.0.1:8767'
    os.environ['RISKLENS_API_URL']=env['RISKLENS_API_URL']
    with (output/'api.txt').open('x') as stream:
        process=subprocess.Popen([sys.executable,'-m','uvicorn','api.main:app','--host','127.0.0.1','--port','8767'],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=env['RISKLENS_API_URL'],timeout=3,trust_env=False) as client:
                deadline=time.monotonic()+40
                while True:
                    try:
                        if client.get('/health').status_code==200:break
                    except httpx.HTTPError:pass
                    assert time.monotonic()<deadline and process.poll() is None
                    time.sleep(.2)
                items=client.get('/evaluations').json()['items'];assert items and items[0]['simulated']
                result=client.get('/evaluations/'+items[0]['evaluation_id']).json()
                assert len(result['records'])==150 and result['real_model_quality_status']=='BLOCKED'
                import streamlit as st
                from streamlit.testing.v1 import AppTest
                inspect.stack();st.empty()
                app=AppTest.from_file(str(ROOT/'frontend/app.py'),default_timeout=60).run()
                app.radio[0].set_value('Evaluation').run()
                assert not app.exception and any('SYNTHETIC' in w.value and 'BLOCKED' in w.value for w in app.warning)
                evidence={'status':'EXECUTED','transport':'actual localhost HTTP, no API monkeypatch',
                    'evaluation_id':items[0]['evaluation_id'],'arm_records':len(result['records']),
                    'warning_rendered':True,'quality_improvement_claim':result['quality_improvement_claim'],
                    'sections':[e.label for e in app.expander],'real_provider_status':'BLOCKED'}
                (output/'runtime.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence))
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
