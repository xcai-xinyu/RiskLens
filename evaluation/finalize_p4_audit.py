"""Seal partial P4 engineering evidence without approving H4 or real-provider checks."""
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'evaluation/audit/P4/attempt-001'
FILES=['README.md','app/service.py','api/main.py','frontend/app.py','tests/test_p3.py','tests/test_p4.py',
       'evaluation/run_regression.py','evaluation/regression/cases.jsonl','evaluation/regression/checker_migrations_p4_v1.json',
       'evaluation/p4_dataset.py','evaluation/p4_numeric_revision.py','evaluation/scorers.py',
       'evaluation/runner.py','evaluation/repository.py','evaluation/run_eval.py','evaluation/run_p4_check.py',
       'evaluation/p4_runtime.py','evaluation/p4_integrity.py','evaluation/p4_collect_runtime.py',
       'evaluation/p4_process.py','evaluation/prompts/__init__.py','evaluation/prompts/raw_p4_v1.txt',
       'docs/P4_EVALUATION.md','docs/decisions/0005-p4-evaluation.md','evaluation/finalize_p4_audit.py']


def write(name,text):
    with (OUT/name).open('x') as f:f.write(text)


def main():
    assert not (OUT/'SHA256SUMS').exists()
    commands=[json.loads(l) for l in (OUT/'commands.jsonl').read_text().splitlines()]
    lookup={Path(c['output']).stem:c for c in commands}
    required=['targeted-fresh','regression','full-suite','demo-final-1','demo-final-2','collect-reruns',
              'failure-probe','bounded-cli','api-ui-http','history-integrity','lint','compile','diff-check','git-status','provider-presence']
    assert all(lookup[n]['exit_code']==0 for n in required)
    tests={}
    for name in ('targeted-fresh','regression','full-suite'):
        suites=ElementTree.parse(OUT/f'artifacts/{name}.xml').getroot()
        counts={key:sum(int(s.get(key,0)) for s in suites.iter('testsuite')) for key in ('tests','errors','failures','skipped')}
        assert counts['errors']==counts['failures']==counts['skipped']==0;tests[name]=counts
    paths=FILES+[str(p.relative_to(ROOT)) for p in sorted((ROOT/'evaluation/datasets').glob('p4-*/*')) if p.is_file()]
    changed=[{'path':p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in paths]
    previous=json.loads((ROOT/'evaluation/audit/P3/attempt-001/artifacts/worktree_manifest.json').read_text())
    all_paths=sorted({e['path'] for e in previous}|set(paths))
    worktree=[{'path':p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in all_paths]
    worktree_hash=hashlib.sha256(json.dumps(worktree,sort_keys=True).encode()).hexdigest()
    for path in paths:
        dest=OUT/'artifacts/source'/path;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/path,dest)
    write('changed_files.json',json.dumps(changed,indent=2)+'\n')
    write('artifacts/worktree_manifest.json',json.dumps(worktree,indent=2)+'\n')
    write('P4_commands.txt','\n'.join(json.dumps(c,ensure_ascii=False) for c in commands)+'\n')
    write('P4_test_output.txt','\n\n'.join(f'=={n}==\n'+(OUT/f'artifacts/{n}.txt').read_text() for n in ('targeted-fresh','regression','full-suite','lint','history-integrity')))
    known='''# P4 known issues / unverified requirements

Engineering checks passed; the P4 research stage is BLOCKED, not DONE, not WAITING_HUMAN/PASS. H4 is NOT_REQUESTED. No P5 begins.

Required outstanding evidence:
- Genuine provider A/B/C/D experiments have not run. Environment-presence check records variable names only; no secret values or .env read/printed. Provider setup is described in docs/P4_EVALUATION.md. Demo output is not provider PASS.
- Real-company held-out evaluation and full five-category benchmark coverage are not complete. Ten dev questions reuse the real Coinbase document and liquidity/regulatory facts; twenty new held-out cases are explicitly fictitious liquidity snapshots. They validate mechanisms, not generalization.
- Every annotation is an agent-authored PROPOSED_H4 candidate; there is no invented human gold or H4 approval. Exact matching cannot settle genuine model paraphrases; unknowns remain unresolved and need independent adjudication. Scorer output is provisional.
- There is no quality-improvement claim. Constructed semantic fixtures intentionally contain errors; filtering changes denominators. Completeness and false abstention must be reviewed alongside correctness. Repair is separate from the primary experiment.
- Latency is actual arm-incremental execution and shared baseline generation; old retrieval/embedding latency/cost are not measured again. Usage/cost unknown is null. Real adapter usage aggregation is unit-tested with explicitly synthetic metadata, not live tokens.
- Initial standalone UI test hit a WSL first-draw AppTest timeout. Public Streamlit first-element initialization now precedes the widget deadline; final UI test/smoke passed. Two captured targeted failures came from a newly added test using an invalid dummy dataset path; the test path was corrected and fresh targeted/regression/full runs passed. Historical failed outputs remain present.
- Preliminary dataset v1 lacked numeric coverage; v2 was published before experiments with independent numeric/edge references. Both versions remain immutable. No labels changed to obtain a better measured score.
- P3 Evaluation placeholder assertion legitimately migrated to P4 empty-result behavior. Old source/hash stays sealed; versioned migration checks old/current hashes. All P0–P2 contracts/tests/formulas/Verifier and all previous sealed artifacts are unchanged.
- No configured mypy/pyright; dedicated type check N/A, Ruff and compileall passed.
'''
    write('P4_known_issues.md',known)
    preparation='''# H4 preparation — NOT READY FOR APPROVAL

H3 browser review was approved and recorded in ../approvals/H3-20261010T170940835520.json. H4 is not approved or requested. P4 engineering passes but required real research evidence remains blocked.

Read ../../../../docs/P4_EVALUATION.md for protocol, commands, safe configuration and remaining benchmark work. Review candidate dataset/annotations/snapshots in artifacts/source/evaluation/datasets/p4-v2/. Each annotation includes original quote/page/section or synthetic source location; all are PROPOSED_H4.

Inspect artifacts/run1/result.json and artifacts/run2/result.json for all thirty cases and 150 arm records each. artifacts/aggregate_table.md shows numerators/denominators, not improvement claims. artifacts/rerun_comparison.json confirms paired outputs/scores/protocol/engine hashes reproduce; timing differences remain measured. artifacts/failure_probe/ retains thirty cases and the isolated raw-arm execution failure. Do not combine that fault experiment with primary results.

Sampling suggestions: dev-00 real-source fact/citation; heldout-00 normal ratio with inputs 200/100 million USD FY:2024 = 2; heldout-01 zero denominator and overstatement narrowing; heldout-02 currency mismatch/contradiction; heldout-03 missing metric input/fabricated citation; heldout-04 period mismatch/after-cutoff rejection. Check the original draft, B/C/D shared hash, independent candidate judgment, decision, score, Repair lineage and one-round budget. Verify filtering did not inflate correctness by hiding completeness loss.

UI Evaluation reads the actual persisted run p4-20261010093510918874 through ResearchService/Repository. API/UI HTTP smoke evidence is artifacts/http-runtime/runtime.json. This is simulated/synthetic evidence and browser testing supplies no substantive gold approval.

Before H4 can approve research conclusions, complete genuine provider experiments, real-company held-out/full category coverage, manual gold/source/numeric review and unknown-paraphrase adjudication. No P5 or deployment is authorized by this package.
'''
    write('H4_preparation.md',preparation)
    commit=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    audit={'stage':'P4','attempt':'attempt-001','timestamp':datetime.now(UTC).isoformat(),
        'engineering_status':'PASS','technical_status':'BLOCKED','gate_status':'NOT_REQUESTED','final_status':'BLOCKED',
        'human_gate_required':'H4','commit':commit,'worktree_hash':worktree_hash,
        'approval_basis':'evaluation/audit/approvals/H3-20261010T170940835520.json',
        'implemented':['Frozen candidate datasets/protocol','Independent exact-reference scorer','Paired A/B/C/D plus separate bounded Repair','Immutable per-case/aggregate/failure outputs','Rerun comparison','Readonly ResearchService/API/Streamlit Evaluation viewer','Hard process-group timeout'],
        'changed_files':changed,'commands':commands,'tests':tests,
        'acceptance_checks':{'engineering_tests':'PASS','paired_snapshot_identity':'PASS','deterministic_rerun':'PASS',
            'failure_retention':'PASS','repair_budget':'PASS','api_ui_http':'PASS','prior_history_integrity':'PASS',
            'real_provider_A_B_C_D':'BLOCKED','real_company_heldout':'NOT_VERIFIED','full_five_category_benchmark':'NOT_VERIFIED',
            'human_gold_validation':'PENDING_H4','research_quality_improvement':'NOT_CLAIMED','dedicated_type_checker':'N/A'},
        'blocked_checks':['Real-provider evaluation','Real-company heldout/full category coverage','Substantive H4 gold/source review'],
        'findings':{'Critical':[],'Major':['Required research-validity benchmark and provider evidence incomplete'],'Minor':['Exact-reference unresolved paraphrases','Measured replay latency excludes original retrieval']},
        'artifacts_inspected':['Prior P0–P3 seals','H3 approval','Frozen dataset manifests','Two 150-record primary runs','Failure probe','Runtime and test output'],
        'quality_improvement_claim':None,'simulated':True,'real_provider_semantic_status':'BLOCKED','p5_started':False,
        'next_action':'Complete separate real-provider/real-company benchmark work and substantive H4 review; do not approve blocked checks as PASS'}
    write('P4_audit.json',json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    files=sorted(p for p in OUT.rglob('*') if p.is_file() and p.suffix not in ('.sqlite','.db') and '__pycache__' not in p.parts)
    write('SHA256SUMS',''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(OUT))+'\n' for p in files))
    print(json.dumps({'engineering_status':'PASS','technical_status':'BLOCKED','final_status':'BLOCKED','gate_status':'NOT_REQUESTED','tests':tests,'sealed_files':len(files)}))


if __name__=='__main__':main()
