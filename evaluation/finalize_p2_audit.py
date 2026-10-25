"""Seal a completed P2 attempt with actual evidence, no human decisions inferred."""
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))

from app.p2_contracts import P2State
from app.reporting import render_json, render_markdown
from app.storage.repository import Repository

A=ROOT/'evaluation/audit/P2/attempt-001'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(name,text):
    with (A/name).open('x') as f:
        f.write(text)


def stats(name):
    suites=ET.parse(A/'artifacts'/name).getroot().iter('testsuite')
    total=failed=errors=skipped=0
    for s in suites:
        total+=int(s.attrib.get('tests',0)); failed+=int(s.attrib.get('failures',0))
        errors+=int(s.attrib.get('errors',0)); skipped+=int(s.attrib.get('skipped',0))
    return {'passed':total-failed-errors-skipped,'failed':failed+errors,'skipped':skipped}


def main():
    assert not (A/'SHA256SUMS').exists(), 'sealed_attempt'
    commands=[json.loads(line) for line in (A/'commands.jsonl').read_text().splitlines()]
    required=['full-suite-seal','regression-seal','examples-seal','cli-p2-final','cli-p2-cutoff',
              'cli-p2-markdown','cli-p0-compat','cli-p1-compat','lint-seal','diff-completion',
              'history-contract-integrity','live-missing-config']
    by_name={c['name']:c for c in commands}
    assert all(by_name[n]['exit_code']==by_name[n]['expected_exit_code'] for n in required)
    tests=stats('full-suite-seal.xml');regression=stats('regression-seal.xml')
    assert tests['failed']==regression['failed']==tests['skipped']==regression['skipped']==0
    state=P2State.model_validate_json((A/'artifacts/cli-p2-final.txt').read_text())
    assert not state.errors and len(state.report.claims)==5
    assert all(v.status=='supported' for v in state.verification_results)
    assert state.report.summary=='\n'.join(c.text for c in state.report.claims)
    assert all(m.status=='missing_input' and m.value is None for m in state.metrics)
    for name,expected in state.metadata['p2_content_hashes'].items():
        assert sha(ROOT/name)==expected, name
    write('artifacts/final_report.json',render_json(state.report))
    write('artifacts/final_report.md',render_markdown(state.report))
    repo=Repository(Path(state.documents[0].local_path).parent)
    trace=[{'claim':c.model_dump(mode='json'),'verification':next(v.model_dump(mode='json')
            for v in state.verification_results if v.claim_id==c.claim_id),
            'references':[{'evidence':e.model_dump(mode='json'),'trace':repo.trace(e,state.request.as_of_date)}
                for e in state.evidence if e.evidence_id in c.evidence_ids],
            'human_decision':None} for c in state.draft_claims]
    repo.close()
    write('artifacts/final_claim_trace.json',json.dumps(trace,indent=2)+'\n')
    cutoff=json.loads((A/'artifacts/cli-p2-cutoff.txt').read_text())
    assert not cutoff['claims'] and cutoff['as_of_date']=='2025-02-12'
    assert 'after_cutoff' in [g['code'] for g in cutoff['gaps']]
    examples=json.loads((A/'artifacts/seal-examples/synthetic_state.json').read_text())
    assert len(examples['lineage'])==6 and examples['repair_count']==1
    assert {x['verification']['status'] for x in examples['lineage']}=={
        'supported','partially_supported','unsupported','contradicted','unverifiable'}
    write('artifacts/development_failures.json',json.dumps([
        {'name':'preaudit-targeted-first','argv':['.venv/bin/python','-m','pytest','tests/test_p2.py','-q'],
         'exit_code':130,'started_at':None,'ended_at':None,'counts':None,
         'note':'Unbounded preliminary diagnostic interrupted with Ctrl-C after routing/shutdown hang; tool excerpt showed 36 dots and seven failures. Exact timestamps/counts not captured.'},
        {'name':'preaudit-targeted-x','argv':['timeout','--kill-after=5s','120s','.venv/bin/python','-m','pytest',
             'tests/test_p2.py','-x','-vv','-o','faulthandler_timeout=40'],
         'exit_code':1,'started_at':None,'ended_at':None,'passed':36,'failed':1,
         'observed_error':'TypeError: dict() got multiple values for keyword argument conflicts',
         'resolution':'Fixed new helper dictionary construction; expectations were not relaxed.'}
    ],indent=2)+'\n')
    baseline=json.loads((ROOT/'evaluation/audit/P1/attempt-20261007T201542/artifacts/worktree_manifest.json').read_text())
    old={x['path']:x['sha256'] for x in baseline}
    candidates=set(old)
    for folder in ('app','tests','docs','evaluation'):
        for p in (ROOT/folder).rglob('*'):
            relative=str(p.relative_to(ROOT))
            if p.is_file() and p.suffix in ('.py','.txt','.md','.json','.jsonl') and '/__pycache__/' not in relative and not relative.startswith('evaluation/audit/'):
                candidates.add(relative)
    candidates.update(['main.py','README.md','requirements.txt','requirements.in','pyproject.toml','.gitignore','.env.example','AGENTS.md','SPEC.md'])
    manifest=[{'path':name,'sha256':sha(ROOT/name)} for name in sorted(candidates)]
    changed=[x['path'] for x in manifest if old.get(x['path'])!=x['sha256']]
    write('artifacts/worktree_manifest.json',json.dumps(manifest,indent=2)+'\n')
    for name in changed:
        destination=A/'artifacts/source'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as f:
            f.write((ROOT/name).read_bytes())
    worktree_hash=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    # In a pre-existing uncommitted repository, the hash manifest covers untracked source too.
    diff=subprocess.run(['git','diff','--','app','main.py','README.md','requirements.txt'],cwd=ROOT,capture_output=True,text=True,check=False)
    write('artifacts/final_diff.txt',diff.stdout)
    status=subprocess.run(['git','status','--short','--untracked-files=all'],cwd=ROOT,capture_output=True,text=True,check=False)
    write('artifacts/final_git_status.txt',status.stdout)
    commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=False).stdout.strip()
    metrics=examples['metrics']
    lines=['# H2 substantive human review — decisions left blank','',
        'P2 only. Human approval has not been recorded. Demo semantic decisions are SIMULATED; these are engineering fixtures, not measured real-model quality.',
        '', '## Representative metrics — SYNTHETIC, not Coinbase data',
        'Source: artifacts/seal-examples/synthetic_source.txt; trace: artifacts/seal-examples/synthetic_trace.json.',
        'Each source input uses million USD. All non-OCF pairs use FY:2024; OCF compares FY:2024 with FY:2023.',
        '| Metric | Formula | Source inputs | Python Decimal result | Human recalculation / decision |',
        '|---|---|---|---|---|']
    for m in metrics:
        detail='; '.join(f"{i['name']}={i['value']} {i['unit']} {i['currency']} {i['period']} ({i['evidence_id']})" for i in m['inputs'])
        lines.append(f"| {m['name']} | {m['formula']} | {detail} | {m['value']} | **blank** |")
    lines += ['', 'Check unit conversion (200 million / 100 million = 2), year-over-year OCF ((60−40)/abs(40)=0.5), definitions (cash versus resources, EBIT versus EBITDA), and the null/not_applicable edge cases in test output.',
        '', '## At least five substantive Verifier decisions — SYNTHETIC, SIMULATED',
        'Source title: SYNTHETIC P2 review source 0; section: Text body; page: null. Full original quote, document ID, evidence ID and source offsets are in synthetic_trace.json.',
        'Source 1 supplies a same-period cash disagreement and untrusted instructions; review the original text and conflict handling test without treating instructions as permissions.',
        'Original drafts and failed decisions remain in synthetic_state.json and report lineage. All human entries below are blank.', '']
    for item in examples['lineage']:
        c,v=item['claim'],item['verification']
        lines += [f"### {c['claim_id']} / round {item['round']}",f"Claim ({c['claim_type']}): {c['text']}",
            f"Evidence IDs: {', '.join(c['evidence_ids'])}",f"Simulated decision: {v['status']}; {v['explanation']}",
            f"Conflicts: {v['conflicts']}",'Human entailment / inference-strength decision: **blank**','']
    lines += ['Check c2 unsupported and c3 contradicted are excluded; c4 unverifiable becomes a gap. c1 must be narrowed and reverified before admission. Its original partially_supported decision must remain visible.',
        '', '## Real PDF demo — simulated semantic model decisions',
        'Source title: Coinbase Q4 and Full Year 2024 Shareholder Letter; published_at 2025-02-13. All five draft claims and original source mappings are in artifacts/final_claim_trace.json.',
        'Final JSON: artifacts/final_report.json; Markdown: artifacts/final_report.md. Metrics remain missing_input; risk remains UNKNOWN/partial. Do not treat simulated Verifier decisions as measured model performance.',
        '', '## Date and integrity review',
        'Cutoff 2025-02-12 excludes the document published 2025-02-13. See artifacts/cli-p2-cutoff.txt. History seals and protected P0/P1 contracts are checked in artifacts/history-contract-integrity.txt.',
        '', '## Human gate', 'Gate: H2', 'Reviewer/date: **blank**', 'Substantive decision: **blank**',
        'Required fixes/disagreements: **blank**', 'P3 has not started.']
    write('H2_review.md','\n'.join(lines)+'\n')
    write('P2_known_issues.md','''# P2 limitations

- Real provider semantic verification is BLOCKED: injected OPENAI_API_KEY and LLM_MODEL unavailable. Real fixed-document P2 additionally needs EMBEDDING_MODEL; live additionally needs TAVILY_API_KEY. Configure names through secure IDE/process environment injection, never print values. The application does not load .env.
- Semantic fixture decisions validate orchestration, hard gates, repair and rendering; they do not prove real entailment quality or semantic injection resistance.
- Coinbase demo leaves formula inputs missing instead of fabricating them. Successful numeric review examples use explicitly synthetic sources.
- Risk levels conservatively remain UNKNOWN with a gap when reviewed category risk coverage is insufficient; reports remain partial/insufficient_evidence. No completed v1 claim.
- Decimal precision is 34 with isolated ROUND_HALF_EVEN context. Monetary units are explicit; OCF supports adjacent fiscal years / same-quarter year-over-year. Other layouts abstain; extreme numeric magnitudes/digit counts are bounded.
- Source-token checks are necessary checks; precise definition/value association also requires semantic review. Provisional support weights are not calibrated probabilities; corroboration remains zero without proven independent support.
- The fixed PDF is a 2026 snapshot of a dated 2025 original, not independently archived on publication day.
- No type checker is configured. Ruff, schema validation and behavioral tests ran.
- Earlier synthetic reports inherited the P1 real-document label; final seal-examples are explicitly labeled SYNTHETIC FIXTURE DOCUMENTS. Earlier artifacts remain preserved.
- Development failures remain visible: helper construction/lint failures and restricted-WSL synchronous conditional routing/runner-shutdown hang. Async P2 routes resolved the runtime failure; successful final suite and runtime artifacts are retained. The initial unbounded diagnostic was interrupted, not recorded as a successful run.
- P3–P5 and human H2 approval remain out of scope / pending. Existing audits and approvals are unchanged.
''')
    write('P2_commands.txt','\n'.join(f"{c['name']}: {json.dumps(c['argv'])}\n  cwd={c['cwd']}\n  start={c['started_at']} end={c['ended_at']} exit={c['exit_code']} expected={c['expected_exit_code']} artifact={c['artifact']}" for c in commands)+'\n')
    write('P2_test_output.txt','\n'.join('== '+n+' ==\n'+(A/by_name[n]['artifact']).read_text() for n in ['full-suite-seal','regression-seal','targeted-final'])+'\n')
    checks=[{'name':n,'status':'PASS','evidence':[by_name[n]['artifact']]} for n in required]
    checks += [{'name':'real_provider_semantic_verification','status':'BLOCKED',
                'required_for_offline_P2_gate':False,'evidence':['artifacts/provider-smoke.txt']},
               {'name':'real_semantic_injection_resistance','status':'BLOCKED','required_for_offline_P2_gate':False,
                'reason':'No valid injected provider configuration; fixtures are not real semantic evidence'},
               {'name':'real_fixed_document_and_live_end_to_end','status':'NOT_VERIFIED','required_for_offline_P2_gate':False,
                'reason':'Provider/embedding/search runtime configuration unavailable'},
               {'name':'configured_type_check','status':'N/A','reason':'No type checker configured'}]
    artifacts=[{'path':str(p.relative_to(A)),'sha256':sha(p)} for p in sorted(A.rglob('*')) if p.is_file()]
    audit={'stage':'P2','task_id':'P2-metrics-verifier-repair-report','attempt_id':'attempt-001',
        'timestamp':datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),'timezone':'Asia/Singapore',
        'commit':commit,'dirty':bool(status.stdout),'worktree_hash':worktree_hash,'changed_files':changed,
        'commands':commands,'tests':tests,'targeted_tests':stats('targeted-final.xml'),'regression':regression,
        'activated_regressions':[json.loads(line)['case_id'] for line in (ROOT/'evaluation/regression/cases.jsonl').read_text().splitlines()],
        'artifacts':artifacts,'acceptance_checks':checks,
        'findings':{'critical':[],'major':[],'minor':['Real semantic quality not verified','Synthetic numeric examples; real demo missing metrics','Risk category coverage insufficient; UNKNOWN','Conservative comparable-period and corroboration support']},
        'resolved_findings':['New test helper duplicate keyword construction','Lint issues','Synchronous conditional route / restricted WSL executor shutdown hang','Inherited real-document label corrected for synthetic P2 fixtures'],
        'blocked_checks':['Real semantic verification: OPENAI_API_KEY and LLM_MODEL missing','Real P2 also requires EMBEDDING_MODEL; live requires TAVILY_API_KEY','Real semantic injection resistance not exercised'],
        'runtime_modes':['P0 demo compatibility','P1 real-fixed-document demo compatibility','P2 real-fixed-document demo with simulated semantic responses','P2 synthetic metric/adversarial demo','P2 cutoff exclusion','P2 missing-config live rejection'],
        'real_provider_modes_executed':False,'technical_status':'PASS','task_status':'DONE',
        'human_gate_required':'H2','gate_status':'PENDING','final_status':'WAITING_HUMAN','approval':None,
        'human_review':{'package':'H2_review.md','sample_ids':['c1','c2','c3','c4','c5'],
                        'human_metric_recalculations':None,'human_entailment_decisions':None},
        'prerequisite':{'gate':'H1','status':'APPROVED','record':'evaluation/audit/approvals/H1-20261008T160425634504.json'},
        'next_stage_started':False}
    write('P2_audit.json',json.dumps(audit,indent=2)+'\n')
    files=[p for p in sorted(A.rglob('*')) if p.is_file()]
    write('SHA256SUMS',''.join(f'{sha(p)}  {p.relative_to(A)}\n' for p in files))
    for line in (A/'SHA256SUMS').read_text().splitlines():
        expected,name=line.split('  ',1)
        assert sha(A/name)==expected
    print(json.dumps({'audit':str((A/'P2_audit.json').relative_to(ROOT)),
        'technical_status':'PASS','gate_status':'PENDING','final_status':'WAITING_HUMAN',
        'tests':tests,'regression':regression,'sealed_files':len(files),'audit_sha256':sha(A/'P2_audit.json')}))


if __name__=='__main__':
    main()
