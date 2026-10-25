"""Seal attempt-002; retain reviewed attempt-001 unchanged, never approve H2."""
import difflib
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
A=ROOT/'evaluation/audit/P2/attempt-002'
OLD=ROOT/'evaluation/audit/P2/attempt-001'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name,text):
    p=A/name
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x') as f:
        f.write(text)


def stats(name):
    passed=failed=skipped=0
    for suite in ET.parse(A/f'artifacts/{name}.xml').getroot().iter('testsuite'):
        errors=int(suite.get('errors','0'))+int(suite.get('failures','0'))
        skip=int(suite.get('skipped','0'))
        passed+=int(suite.get('tests','0'))-errors-skip
        failed+=errors;skipped+=skip
    return {'passed':passed,'failed':failed,'skipped':skipped}


def main():
    assert not (A/'SHA256SUMS').exists()
    # Immutable preservation includes every byte of reviewed evidence and source snapshots.
    for line in (OLD/'SHA256SUMS').read_text().splitlines():
        expected,name=line.split('  ',1)
        assert sha(OLD/name)==expected,name
    commands=[json.loads(line) for line in (A/'commands.jsonl').read_text().splitlines()]
    by_name={c['name']:c for c in commands}
    required=['targeted-final','full-suite','regression','runtime-final','lint-final',
              'history-integrity','python-version','diff-check']
    assert all(by_name[n]['exit_code']==by_name[n]['expected_exit_code'] for n in required)
    tests={name:stats(name) for name in ('targeted-final','full-suite','regression')}
    assert all(s['failed']==s['skipped']==0 for s in tests.values())
    state=json.loads((A/'artifacts/examples/synthetic_state.json').read_text())
    c1=next(v for v in state['verification_results'] if v['claim_id']=='c1')
    original=next(c for c in state['draft_claims'] if c['claim_id']=='c1')
    probe=json.loads((A/'artifacts/examples/c1_narrowing_probe.json').read_text())
    assert c1['status']=='unverifiable' and c1['evidence_support_score'] is None
    assert not c1['checks']['no_overstatement'] and not c1['checks']['no_conflicts']
    assert not c1['checks']['semantic_support'] and len(c1['conflicts'])==1
    assert probe['verification']['status']=='unverifiable'
    assert probe['verification']['checks']['no_overstatement']
    assert not probe['verification']['checks']['no_conflicts']
    assert [c['claim_id'] for c in state['report']['claims']]==['c5']
    assert state['report']['summary']=='Current assets are 200 million USD FY:2024.'
    assert state['repair_count']==0 and state['budget']['llm_calls']<=10
    assert probe['probe_model_calls_after']<=probe['max_llm_calls']
    trace=json.loads((A/'artifacts/examples/synthetic_trace.json').read_text())
    assert len(trace)==2
    evidence=state['evidence']
    assert all(e['evidence_id'] in c1['conflicts'][0] for e in evidence)
    provider=json.loads((A/'artifacts/provider-smoke.txt').read_text())
    assert provider['status']=='BLOCKED' and by_name['provider-smoke']['exit_code']==3
    preservation={'reviewed_attempt':str(OLD.relative_to(ROOT)),
        'audit_sha256':sha(OLD/'P2_audit.json'),'seal_sha256':sha(OLD/'SHA256SUMS'),
        'all_sealed_files_verified_unchanged':True,
        'H2_approval':None,'review_outcome':'User reported two defects; H2 NOT approved',
        'defects':['c1 overstatement explanation but no_overstatement=true',
                   'same-period 20/10 cash disagreement omitted from conflicts'],
        'authorization':'User explicitly requested P2 corrections, new attempt, no P3 and no approval request before corrected review'}
    write('review_findings.json',json.dumps(preservation,indent=2)+'\n')
    write('artifacts/c1_corrected_review.json',json.dumps({'original_claim':original,
        'verification':c1,'sources':trace,'narrowing_probe':probe,
        'final_claim_ids':['c5'],'final_summary':state['report']['summary'],
        'gaps':state['report']['gaps'],'repair_count':state['repair_count'],
        'budget':state['budget'],'limits':state['metadata']['config'],
        'human_decision':None},indent=2)+'\n')
    review=['# H2 corrected c1 review — attempt-002','',
        'H2 is NOT approved. Human decision fields remain blank. P3 has not started.',
        'Sources and semantic decisions are synthetic/simulated. Decimal arithmetic, source tracing, conflict matching, adjudication, report admission and budget counts actually executed.',
        '', '## Original c1',f"Claim ({original['claim_type']}): {original['text']}",
        f"Evidence IDs: {original['evidence_ids']}; metric IDs: {original['metric_ids']}",
        '', '## Exact source observations']
    for e in evidence:
        doc=next(d for d in state['documents'] if d['document_id']==e['document_id'])
        review += [f"- {doc['title']}; {e['evidence_id']}; document_id={doc['document_id']}; section={e['section']}; page={e['page']}; published_at={doc['published_at']}",
                   f"> {e['quote']}"]
    review += ['', '## Corrected structured result',
        '```json',json.dumps(c1,indent=2),'```',
        'Simulated semantic finding: partially_supported, overstatement=true. Deterministic same-period cash conflict takes precedence; final status is unverifiable, score null. Both source locations and exact observations are retained. The original guarantee and c1 are excluded from final claims/summary.',
        '', '## Narrowing and re-verification probe',
        f"Narrowed text: {probe['narrowed']['text']}",
        'This is an independent review probe, not a workflow repair. Removing the guarantee makes no_overstatement=true, but no_conflicts remains false and status remains unverifiable. It does not resolve the 20/10 source disagreement.',
        f"Actual workflow repair count: {state['repair_count']} / {state['metadata']['config']['max_repair_rounds']}",
        f"Workflow LLM calls: {state['budget']['llm_calls']} / {state['metadata']['config']['max_llm_calls']}; tools: {state['budget']['tool_calls']} / {state['metadata']['config']['max_tool_calls']}",
        f"Probe's cumulative LLM count: {probe['probe_model_calls_after']} / {probe['max_llm_calls']} (includes the original {probe['model_calls_before']} workflow calls and one independent re-verification)",
        'No repair was fabricated for the unresolved conflict. A separate one-source repair test still requires narrowing and re-verification.',
        '', '## Final report integrity',
        'Final claim IDs: c5 only. c1, c2, c3, c4 remain excluded, with failures/gaps traceable.',
        'Final summary:',f"> {state['report']['summary']}",
        'Report remains partial / UNKNOWN.',
        '', '## Evidence links',
        '- artifacts/c1_corrected_review.json — complete mapping',
        '- artifacts/examples/c1_narrowing_probe.json — independent re-verification',
        '- artifacts/examples/synthetic_state.json — original lineage, decisions and budgets',
        '- artifacts/examples/synthetic_trace.json — original source locations',
        '- artifacts/examples/synthetic_report.json / synthetic_report.md — final disposition',
        '- artifacts/targeted-final.txt, full-suite.txt, regression.txt — actual test outputs',
        '- artifacts/provider-smoke.txt — BLOCKED; missing OPENAI_API_KEY and LLM_MODEL',
        '', '## Human review — blank',
        'Reviewer / date: **blank**',
        'Overstatement consistency: **blank**',
        'Cash conflict preservation / final disposition: **blank**',
        'H2 decision and required changes: **blank**',
        'Await corrected review. Approval is not requested in this turn.']
    write('H2_review.md','\n'.join(review)+'\n')
    write('P2_known_issues.md','''# P2 attempt-002 limitations and changes

The two reviewed c1 defects are corrected: semantic overstatement flag, explicit guarantee guard and same-period traceable cash conflicts. Final flags reflect final adjudication; unresolved conflict excludes even the narrowed claim. Both source texts/claims/numeric expectations remain unchanged. v1 fixture and attempt-001 remain sealed and preserved; v2 records the user-authorized correction.

Old repair tests inadvertently used conflicting sources while expecting admission. Their original failures are preserved in baseline-failures.txt. Repair-specific tests now explicitly use a nonconflicting precondition without relaxing repair/reverification assertions; new two-source tests require conflict retention, null score, gap and exclusion, including after narrowing. All existing v1 regression cases/hashes remain; the v2 correction case is appended.

The matcher recognizes explicit cash is/of numeric monetary clauses with identical currency and FY/quarter period, normalizes registered units and requires source trace/date validity. Other wording, company attribution and financial definitions remain semantic review tasks. It does not equate cash with USD resources or cash-and-cash-equivalents. No general semantic accuracy claim is made.

Real semantic provider smoke remains BLOCKED: OPENAI_API_KEY and LLM_MODEL are not injected. Real document modes additionally need EMBEDDING_MODEL; live also needs TAVILY_API_KEY. Use secure process/IDE environment injection; do not print values. No .env was read. Synthetic semantic decisions do not prove real-provider quality or semantic attack resistance. Numeric examples are synthetic, not Coinbase metrics.

No type checker is configured; configured Ruff and deterministic behavioral checks ran. Baseline assertion failures, an intermediate test syntax error and lint issue remain recorded; final checks must pass before sealing.

H2 is not approved. Final status WAITING_HUMAN; P3–P5 not started. User requested corrected evidence before any new approval request.
''')
    baseline=json.loads((OLD/'artifacts/worktree_manifest.json').read_text())
    old={x['path']:x['sha256'] for x in baseline}
    candidates=set(old)
    for folder in ('app','tests','docs','evaluation'):
        for p in (ROOT/folder).rglob('*'):
            rel=str(p.relative_to(ROOT))
            if p.is_file() and p.suffix in ('.py','.txt','.md','.json','.jsonl') and '/__pycache__/' not in rel and not rel.startswith('evaluation/audit/'):
                candidates.add(rel)
    manifest=[{'path':name,'sha256':sha(ROOT/name)} for name in sorted(candidates)]
    changed=[x['path'] for x in manifest if old.get(x['path'])!=x['sha256']]
    write('artifacts/worktree_manifest.json',json.dumps(manifest,indent=2)+'\n')
    diffs=[]
    for name in changed:
        destination=A/'artifacts/source'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as f:
            f.write((ROOT/name).read_bytes())
        before=OLD/'artifacts/source'/name
        if before.exists():
            diffs.extend(difflib.unified_diff(before.read_text().splitlines(True),(ROOT/name).read_text().splitlines(True),
                         fromfile='attempt-001/'+name,tofile='attempt-002/'+name))
    write('artifacts/reviewed_attempt_diff.txt',''.join(diffs))
    write('P2_commands.txt','\n'.join(f"{c['name']}: {json.dumps(c['argv'])}\ncwd={c['cwd']}\nstart={c['started_at']} end={c['ended_at']} exit={c['exit_code']} expected={c['expected_exit_code']}\nartifact={c['artifact']}" for c in commands)+'\n')
    write('P2_test_output.txt','\n'.join((A/by_name[n]['artifact']).read_text() for n in ('targeted-final','full-suite','regression')))
    artifacts=[{'path':str(p.relative_to(A)),'sha256':sha(p)} for p in sorted(A.rglob('*')) if p.is_file()]
    audit={'stage':'P2','task_id':'P2-H2-c1-overstatement-conflict-fix','attempt_id':'attempt-002',
        'timestamp':datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),'timezone':'Asia/Singapore',
        'commit':subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=False).stdout.strip(),
        'dirty':True,'worktree_hash':hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),
        'changed_files':changed,'commands':commands,'tests':tests['full-suite'],'targeted_tests':tests['targeted-final'],
        'regression':tests['regression'],'artifacts':artifacts,
        'acceptance_checks':[{'name':n,'status':'PASS','evidence':[by_name[n]['artifact']]} for n in required]+[
            {'name':'c1_corrected_flags_conflict_and_exclusion','status':'PASS','evidence':['artifacts/c1_corrected_review.json']},
            {'name':'reviewed_attempt_preserved','status':'PASS','evidence':['review_findings.json']},
            {'name':'real_provider_semantic_verification','status':'BLOCKED','required_for_offline_P2_gate':False,'evidence':['artifacts/provider-smoke.txt']},
            {'name':'configured_type_check','status':'N/A','reason':'No type checker configured'}],
        'findings':{'critical':[],'major':[],'minor':['General semantic interpretation remains provider-dependent','Real-provider semantic quality BLOCKED','Explicit cash conflict matcher has conservative wording scope']},
        'resolved_findings':preservation['defects'],'blocked_checks':['Real semantic provider: OPENAI_API_KEY and LLM_MODEL missing'],
        'runtime_modes':['Synthetic P2 end-to-end with retained two-source conflict','Independent narrowed-text re-verification probe'],
        'real_provider_modes_executed':False,'technical_status':'PASS','task_status':'DONE',
        'human_gate_required':'H2','gate_status':'PENDING','final_status':'WAITING_HUMAN','approval':None,
        'human_review':{'package':'H2_review.md','sample_ids':['c1'],'human_decision':None,
                        'approval_requested':False,'reason':'User requires corrected evidence review before another approval request'},
        'prior_reviewed_attempt':preservation,'next_stage_started':False}
    write('P2_audit.json',json.dumps(audit,indent=2)+'\n')
    files=[p for p in sorted(A.rglob('*')) if p.is_file()]
    write('SHA256SUMS',''.join(f'{sha(p)}  {p.relative_to(A)}\n' for p in files))
    for line in (A/'SHA256SUMS').read_text().splitlines():
        expected,name=line.split('  ',1)
        assert sha(A/name)==expected
    print(json.dumps({'attempt':'attempt-002','technical_status':'PASS','final_status':'WAITING_HUMAN',
        'tests':tests,'sealed_files':len(files),'old_attempt_unchanged':True,'c1_status':c1['status'],
        'no_overstatement':c1['checks']['no_overstatement'],'no_conflicts':c1['checks']['no_conflicts']}))


if __name__=='__main__':
    main()
