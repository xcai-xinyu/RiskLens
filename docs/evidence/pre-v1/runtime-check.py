"""Actual public-only runtime; no model requests or private environment."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

root=Path.cwd(); sys.path.insert(0,str(root)); output=root/'runtime-public-network'; output.mkdir(exist_ok=True)
python=sys.executable
request={'company':'Coinbase','question':'Assess liquidity and regulatory risks with evidence.','as_of_date':'2025-02-13','risk_categories':['liquidity','regulatory'],'mode':'demo'}
records=[]
def command(label,args):
 started=time.monotonic();r=subprocess.run([python,*args],capture_output=True,text=True,timeout=185,check=False)
 (output/(label+'.txt')).write_text(r.stdout+r.stderr)
 records.append({'label':label,'command':['python',*args],'exit_code':r.returncode,'elapsed_seconds':time.monotonic()-started})
 assert r.returncode==0,label
 return r.stdout
cli=['main.py','--stage','p2','--mode','demo','--company','Coinbase','--question',request['question'],'--as-of-date','2025-02-13','--risk-categories','liquidity','regulatory']
raw=command('cli-json',cli+['--format','json']);report=json.loads(raw)
assert report['metadata']['simulated'] is True
(output/'demo-report.json').write_text(raw)
command('cli-markdown',cli+['--format','markdown'])
command('synthetic-history',['evaluation/p3_review_seed.py','--database','data/private/risklens.sqlite'])
api=None;ui=None;logs=[]
# Fixed ports match the README and only bind loopback; fail rather than use an unrelated process.
def start_api():
 import socket
 with socket.socket() as probe:
  probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  probe.bind(('127.0.0.1',8000))
 handle=(output/'api-server.txt').open('a');logs.append(handle)
 p=subprocess.Popen([python,'-m','uvicorn','api.main:app','--host','127.0.0.1','--port','8000'],stdout=handle,stderr=subprocess.STDOUT)
 for _ in range(120):
  if p.poll() is not None: raise AssertionError('api_start_failed')
  try:
   r=httpx.get('http://127.0.0.1:8000/health',timeout=1,trust_env=False)
   if r.status_code==200: return p
  except httpx.HTTPError:pass
  time.sleep(.25)
 raise AssertionError('api_start_timeout')
def stop(p):
 if p and p.poll() is None:
  p.terminate()
  try:p.wait(20)
  except subprocess.TimeoutExpired:p.kill();p.wait()
try:
 api=start_api()
 with httpx.Client(base_url='http://127.0.0.1:8000',timeout=10,trust_env=False) as client:
  assert client.get('/health').json()['status']=='ok'
  response=client.post('/research',json=request);assert response.status_code==202
  identifier=response.json()['run_id'];started=time.monotonic()
  while time.monotonic()-started<185:
   run=client.get('/runs/'+identifier).json()
   if run['status'] not in ('queued','running'):break
   time.sleep(.2)
  assert run['status'] in ('completed','partial'),run
  report=client.get('/reports/'+identifier).json();assert report['metadata']['simulated']
  (output/'api-demo-report.json').write_text(json.dumps(report,indent=2)+'\n')
  assert client.get('/reports/missing').status_code==404
  assert client.get(f'/reports/{identifier}/export?format=markdown').status_code==200
  history=client.get('/reports').json();assert history['total']>=3
  compare=client.post('/reports/compare',json={'run_ids':['h3-synthetic-a','h3-synthetic-b']}).json()
  assert compare['directly_comparable'] is False
  (output/'history-comparison.json').write_text(json.dumps(compare,indent=2)+'\n')
  sample=client.get('/reports/h3-synthetic-a').json();assert sample['metrics'] and sample['metadata']['citations']
  (output/'synthetic-calculation-report.json').write_text(json.dumps(sample,indent=2)+'\n')
  before=hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()
 stop(api);api=start_api()
 with httpx.Client(base_url='http://127.0.0.1:8000',timeout=10,trust_env=False) as client:
  persisted=client.get('/reports/'+identifier).json()
  assert hashlib.sha256(json.dumps(persisted,sort_keys=True).encode()).hexdigest()==before
 # Exercise the real Streamlit widgets against HTTP API, not a mocked client.
 from streamlit.testing.v1 import AppTest
 at=AppTest.from_file(str(root/'frontend/app.py'),default_timeout=45).run(timeout=45)
 assert not at.exception
 at.button[0].click().run(timeout=45)
 for _ in range(180):
  at.run(timeout=45)
  if any(s.value=='Claims & Evidence' for s in at.subheader):break
  time.sleep(.2)
 assert not at.exception
 citations=[e.label for e in at.expander if e.label.startswith('Citation')]
 assert citations,'citation panels missing'
 at.radio[0].set_value('Report History').run(timeout=45)
 # Select explicitly labeled synthetic report for numeric trace.
 at.selectbox[0].set_value('h3-synthetic-a').run(timeout=45)
 assert not at.exception
 calculations=[e.label for e in at.expander if e.label.startswith('Calculation')]
 assert calculations,'metric trace panels missing'
 # Inspect actual widgets before selecting the comparison pair.
 print('UI controls:',[(w.label,w.value) for w in at.multiselect])
 if at.multiselect:
  at.multiselect[0].set_value(['h3-synthetic-a','h3-synthetic-b']).run(timeout=45)
  compare_buttons=[b for b in at.button if 'Compare' in b.label]
  if compare_buttons:compare_buttons[0].click().run(timeout=45)
 assert not at.exception
 assert any('Not directly comparable' in w.value for w in at.warning)
 # Actual server startup/health, separate from AppTest. No screenshot claimed.
 handle=(output/'streamlit-server.txt').open('w');logs.append(handle)
 ui=subprocess.Popen([python,'-m','streamlit','run','frontend/app.py','--server.address','127.0.0.1','--server.port','8501','--server.headless','true','--browser.gatherUsageStats','false'],stdout=handle,stderr=subprocess.STDOUT)
 for _ in range(120):
  if ui.poll() is not None:raise AssertionError('streamlit_start_failed')
  try:
   response=httpx.get('http://127.0.0.1:8501/_stcore/health',trust_env=False,timeout=1)
   if response.status_code==200:break
  except httpx.HTTPError:pass
  time.sleep(.25)
 else:raise AssertionError('streamlit_health_timeout')
 summary={'mode':'demo','simulated_model_decisions':True,'provider_calls':0,'api_submission':202,'run_status':run['status'],'run_id':identifier,'report_status':report['status'],'claims':len(report['claims']),'metric_statuses':[m['status'] for m in report['metrics']],'report_sha256_preserved_after_process_restart':before,'history_directly_comparable':compare['directly_comparable'],'ui_method':'Streamlit AppTest against actual HTTP API + server health; no screenshot/browser visual approval','citation_expanders':citations,'calculation_expanders':calculations,'ui_warnings':[w.value for w in at.warning],'streamlit_health':response.status_code,'commands':records}
 (output/'runtime-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps(summary,indent=2))
finally:
 stop(ui);stop(api)
 for handle in logs:handle.close()
