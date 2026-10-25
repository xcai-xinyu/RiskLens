"""Seal the current P3 attempt once, only after actual required evidence passes."""
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evaluation/audit/P3/attempt-001'
CHANGED=[
 '.streamlit/config.toml','api/__init__.py','api/main.py','app/history.py','app/p3_contracts.py',
 'app/p3_jobs.py','app/service.py','app/storage/run_repository.py','frontend/__init__.py',
 'frontend/app.py','frontend/client.py','main.py','requirements.in','requirements.txt','README.md',
 'docs/P3_REVIEW.md','docs/decisions/0004-p3-surfaces-persistence.md',
 'evaluation/run_regression.py','evaluation/regression/cases.jsonl','evaluation/run_p3_check.py',
 'evaluation/p3_runtime.py','evaluation/p3_review_seed.py','evaluation/p3_integrity.py',
 'evaluation/finalize_p3_audit.py','tests/test_p3.py','tests/fixtures/regression/p3_history_v1.json']


def write(name,text):
    with (OUT/name).open('x') as stream:
        stream.write(text)


def main():
    assert not (OUT/'SHA256SUMS').exists(),'Attempt already sealed'
    commands=[json.loads(line) for line in (OUT/'commands.jsonl').read_text().splitlines()]
    lookup={Path(c['output']).stem:c for c in commands}
    required=['targeted-final','regression','full-suite','runtime-local-network','cli-p3',
              'history-integrity-final','lint','compile','diff-check','git-status']
    assert all(lookup[name]['exit_code']==0 for name in required)
    results={}
    for name in ('targeted-final','regression','full-suite'):
        suites=ElementTree.parse(OUT/f'artifacts/{name}.xml').getroot()
        counts={k:sum(int(s.get(k,0)) for s in suites.iter('testsuite')) for k in ('tests','failures','errors','skipped')}
        assert counts['failures']==counts['errors']==counts['skipped']==0
        results[name]=counts
    runtime=json.loads((OUT/'artifacts/runtime-local-network/runtime-evidence.json').read_text())
    assert runtime['status']=='EXECUTED' and runtime['report_bytes_unchanged']
    assert runtime['provider_semantic_smoke'].startswith('BLOCKED')
    commit=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    manifest=[{'path':p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in CHANGED]
    baseline=json.loads((ROOT/'evaluation/audit/P2/attempt-002/artifacts/worktree_manifest.json').read_text())
    paths=sorted({entry['path'] for entry in baseline}|set(CHANGED))
    worktree=[{'path':p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in paths]
    fingerprint=hashlib.sha256(json.dumps(worktree,sort_keys=True).encode()).hexdigest()
    write('artifacts/worktree_manifest.json',json.dumps(worktree,indent=2)+'\n')
    for p in CHANGED:
        target=OUT/'artifacts/source'/p;target.parent.mkdir(parents=True,exist_ok=True)
        assert not target.exists()
        shutil.copyfile(ROOT/p,target)
    write('changed_files.json',json.dumps(manifest,indent=2)+'\n')
    write('P3_commands.txt','\n'.join(json.dumps(c,ensure_ascii=False) for c in commands)+'\n')
    write('P3_test_output.txt','\n\n'.join(f'== {n} ==\n'+(OUT/f'artifacts/{n}.txt').read_text() for n in ('targeted-final','regression','full-suite','lint','compile','history-integrity-final')))
    known='''# P3 limitations and preserved failed diagnostics

- Real-provider semantic verification: BLOCKED. H2 approved offline/deterministic work only. No real-provider semantic smoke was run in P3, and no key or .env was read/printed. Earlier H2 configuration blockers remain recorded; configure providers using existing P1/P2 instructions for a separately authorized live check.
- Numeric H3 history examples are explicitly synthetic P2 attempt-002 snapshots. Actual UI HTTP submission used demo responses against the real fixed Coinbase PDF; its five ratios correctly remain missing_input/null. Neither is live semantic validation.
- Browser visual/mouse review is NOT VERIFIED by the agent and belongs to pending H3. AppTest executes native Streamlit widgets and renders expander contents; actual API transport/restart and Streamlit HTTP health were exercised.
- Current task stage is coarse research_workflow, queued, report, failed or interrupted. Existing P2 worker does not stream per-node events. In-progress call usage is unknown until completion; no fabricated detailed progress or zero usage.
- One SQLite owner, one worker, eight waiting tasks. Queued/running tasks are marked interrupted/failed on shutdown/restart; explicit resubmission required. No distributed recovery or multi-instance claim.
- Initial widget runs timed out in Streamlit 1.65 first-draw inspect.stack source lookup on WSL. Framework lookup is warmed before AppTest's 15-second widget deadline; all final UI assertions passed. Two optional faulthandler-instrumented runs exited -11 during stack dumping; underlying native diagnostic cause NOT VERIFIED. Standard final targeted, regression and full-suite runs all exited 0. Failed diagnostic outputs are retained, not overwritten.
- First localhost runtime inside the restricted sandbox timed out despite server startup. The approved external-sandbox rerun exercised only localhost API/UI and passed. Initial uv install/freeze similarly required approved access to uv's cache. Existing dependency versions remain unchanged; 15 new packages are pinned.
- Ruff is the configured static check. No mypy/pyright configuration exists, so dedicated type checking is N/A; compileall and schema/runtime tests passed.
- No P4 evaluation, P5 Docker, public deployment, push, transaction, or human approval performed.
'''
    write('P3_known_issues.md',known)
    review=f'''# H3 compact review package — WAITING_HUMAN

Technical checks PASS; gate PENDING. This package does not approve H3.

Use [the complete local review steps](../../../../docs/P3_REVIEW.md) to start the API/UI, submit a demo request, expand citation/calculation records, restart the API and confirm history, then compare the two opt-in synthetic snapshots. UI entry: http://localhost:8501; API: http://127.0.0.1:8000. No services are left running by automated checks.

Actual HTTP/AppTest demo task: `{runtime['run_id']}`. API task status partial, report partial, overall risk UNKNOWN. Claims are from the real fixed PDF; semantic decisions are simulated. [Runtime evidence](artifacts/runtime-local-network/runtime-evidence.json), [report JSON](artifacts/runtime-local-network/http-report.json), [Markdown](artifacts/runtime-local-network/http-report.md), [comparison](artifacts/runtime-local-network/http-comparison.json).

Representative citation: `{runtime['citation_expanders'][0]}`. In Research → Claims & Evidence expand its source title/date/page/quote/document/chunk IDs. In History → h3-synthetic-a expand current_ratio: 200 / 100 million USD FY:2024 = 2, with input evidence IDs; this is SYNTHETIC arithmetic, separate from the actual missing-input demo report.

Runtime proof: Streamlit AppTest called the real localhost API without monkeypatch; POST returned 202, polling terminated, source and metric expanders rendered. API process restarted; exported report bytes remained identical, SHA256 `{runtime['report_sha256']}`. Streamlit HTTP health returned ok. Human mouse expansion/visual review remains pending.

Comparison: h3-synthetic-a as_of_date 2025-02-13 versus h3-synthetic-b 2025-03-01, different synthetic source coverage/corpus/model. Both sides expose prompt/rubric/formula/verification versions; prominent Not directly comparable warning rendered, risk_change_conclusion=null. No automatic trend claim.

UI smoke review: Research and Report History are functional; Evaluation is a P4 placeholder. Executive summary precedes factors, verified fact/inference claims, neutral verification score labels, expandable original citations, formulas/input traces, dedicated gaps/conflicts and audit detail. UNKNOWN gray differs from LOW muted green; execution failure error is explicitly distinct from HIGH risk. History supports pagination, company filter, stored-report selection and two-report comparison.

Tests: 14 P3 targeted, 130 activated deterministic regressions, 139 full offline tests; zero failures/skips in final runs. API tests cover all seven endpoints, 202/pending/404/409/422/503, queue bounds, completed/partial/failed, restart/recovery, exports, pagination, comparison and shared service boundaries. [Final test output](P3_test_output.txt), [commands/exit codes](commands.jsonl), [known limitations](P3_known_issues.md), [changed files/hashes](changed_files.json).

Real-provider semantic smoke remains BLOCKED. Demo/synthetic evidence is not converted to provider PASS. Review and record your own H3 decision; P4/P5 remain unstarted.
'''
    write('H3_review.md',review)
    checks={
      'shared_service_boundary':{'status':'PASS','evidence':'tests/test_p3.py + actual CLI/API/AppTest HTTP'},
      'api_contracts':{'status':'PASS','evidence':'targeted-final.xml'},
      'sqlite_restart_persistence':{'status':'PASS','evidence':'runtime-local-network/runtime-evidence.json'},
      'interrupted_recovery':{'status':'PASS','evidence':'test_sqlite_restart_snapshots_no_chroma_and_history + test_pending_queue_capacity_and_interrupted_run_recovery'},
      'ui_functional_smoke':{'status':'PASS','evidence':'AppTest fixtures + actual localhost HTTP AppTest + Streamlit HTTP health'},
      'non_comparable_history':{'status':'PASS','evidence':'p3_history_v1 fixture, runtime comparison + UI warning'},
      'prior_history_and_contract_preservation':{'status':'PASS','evidence':'history-integrity-final.txt'},
      'activated_regressions':{'status':'PASS','evidence':'regression.xml'},
      'full_offline_suite':{'status':'PASS','evidence':'full-suite.xml'},
      'lint':{'status':'PASS','evidence':'lint.txt'},
      'dedicated_type_checker':{'status':'N/A','reason':'No configured mypy/pyright; compileall and validated schemas exercised'},
      'diff_status':{'status':'PASS','evidence':'diff-check.txt, git-status.txt'},
      'browser_human_review':{'status':'PENDING','evidence':'H3_review.md + docs/P3_REVIEW.md'},
      'real_provider_semantic_smoke':{'status':'BLOCKED','reason':'No live-provider validation; inherited H2 limitation retained'}}
    audit={'stage':'P3','attempt':'attempt-001','timestamp':datetime.now(UTC).isoformat(),
      'technical_status':'PASS','gate_status':'PENDING','final_status':'WAITING_HUMAN',
      'human_gate_required':'H3','commit':commit,'worktree_hash':fingerprint,'changed_files':manifest,
      'approval_basis':'evaluation/audit/approvals/H2-20261008T173833552375.json',
      'approval_scope':'Reviewed offline/deterministic P2; live semantics remain BLOCKED',
      'implemented':['Thin FastAPI seven endpoints','HTTP-only Streamlit UI','ResearchService bounded queue','SQLite run/report authority','Interrupted-to-failed mapping','Version/coverage-aware history comparison','CLI persistence'],
      'commands':commands,'tests':results,'runtime_evidence':runtime,'acceptance_checks':checks,
      'findings':{'Critical':[],'Major':[],'Minor':['Coarse progress and deferred usage counters','WSL first-draw/optional native diagnostic limitation','Human browser visuals pending H3']},
      'blocked_checks':['Real-provider semantic verification'],
      'artifacts_inspected':['P2 attempt-002 seal + H2 approval','all final command outputs','runtime report/comparison/server logs'],
      'p4_started':False,'p5_started':False,'next_action':'APPROVE H3'}
    write('P3_audit.json',json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    files=sorted(p for p in OUT.rglob('*') if p.is_file())
    write('SHA256SUMS',''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(OUT))+'\n' for p in files))
    print(json.dumps({'technical_status':'PASS','gate_status':'PENDING','final_status':'WAITING_HUMAN',
        'sealed_files':len(files),'tests':results,'worktree_hash':fingerprint}))


if __name__=='__main__':
    main()
