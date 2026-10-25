"""Fixed deterministic regression, with only implemented P0/P1/P2/P3 cases activated."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
migrations=json.loads((ROOT/'evaluation/regression/checker_migrations_p4_v1.json').read_text())['migrations']
cases=[json.loads(line) for line in (ROOT/'evaluation/regression/cases.jsonl').read_text().splitlines()]
for case in cases:
    for source in case.get('fixed_sources',[]):
        if hashlib.sha256((ROOT/source['path']).read_bytes()).hexdigest()!=source['sha256']:
            raise SystemExit('Regression source hash mismatch: '+source['path'])
    if 'source_hash' in case and hashlib.sha256((ROOT/case['source']).read_bytes()).hexdigest()!=case['source_hash']:
        migration=next((m for m in migrations if case['case_id'] in m['case_ids'] and m['source']==case['source'] and m['old_sha256']==case['source_hash']),None)
        if migration is None or hashlib.sha256((ROOT/migration['historical_source']).read_bytes()).hexdigest()!=migration['old_sha256'] or hashlib.sha256((ROOT/migration['source']).read_bytes()).hexdigest()!=migration['new_sha256']:
            raise SystemExit('Regression checker source hash mismatch: '+case['source'])
selectors=[
    # Include P0's normal initialization/graph path as well as its failure cases.
    'tests/test_p0.py',
    'tests/test_p1.py::test_date_filter_unknown_dates_and_future',
    'tests/test_p1.py::test_before_publication_runtime',
    'tests/test_p1.py::test_same_url_revision_keeps_distinct_snapshots',
    'tests/test_p1.py::test_dedup_and_same_source_group',
    'tests/test_p1.py::test_index_rebuild_preserves_authority',
    'tests/test_p1.py::test_citation_trace_rejects_invalid_locations',
    'tests/test_p1.py::test_analyst_fabricated_id_rejected',
    'tests/test_p1.py::test_prompt_injection_cannot_grant_tools',
    'tests/test_p1.py::test_ssrf_targets_rejected',
    'tests/test_p1.py::test_public_target_pin_and_redirect_guard',
    'tests/test_p1.py::test_tool_scope_and_offline_network',
    'tests/test_p1.py::test_parser_empty_error_html_and_ocr',
    'tests/test_p1.py::test_model_parse_error_p1_redacts_payload',
    'tests/test_p1.py::test_tool_timeout_is_bounded',
    'tests/test_p1.py::test_total_timeout_kills_native_worker',
    'tests/test_p1.py::test_budget_terminates_p1',
    'tests/test_p1.py::test_repeated_calls_terminate',
]
selectors.append('tests/test_p2.py')
selectors.append('tests/test_p3.py')
selectors.append('tests/test_p4.py')
selectors.append('tests/test_providers.py')
selectors.append('tests/test_environment.py')
selectors.append('tests/test_p4_usage.py')
selectors.append('tests/test_p4_candidate.py')
selectors.append('tests/test_p4_finalization.py')
selectors.append('tests/test_p4_freeze_repairs.py')
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--audit-dir', type=Path)
options, pytest_args = parser.parse_known_args()
env = dict(os.environ)
extra = []
if options.audit_dir:
    directory = options.audit_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env['RISKLENS_REGRESSION_TIMING'] = str(directory / 'timing.json')
    extra = ['-p', 'evaluation.regression_timing', '-vv', '--durations=0',
             '--junitxml=' + str(directory / 'junit.xml')]
command = [sys.executable, '-X', 'importtime', '-m', 'pytest', *selectors, *extra, *pytest_args]
started = time.monotonic()
code = 1
reason = None
try:
    code = subprocess.run(command, cwd=ROOT, env=env, check=False, timeout=600).returncode
except subprocess.TimeoutExpired:
    reason = 'offline_regression_total_timeout_600s'
finally:
    if options.audit_dir:
        (directory / 'entry.json').write_text(json.dumps({
            'command': command, 'selectors': selectors, 'timeout_seconds': 600,
            'elapsed_seconds': time.monotonic() - started, 'exit_code': code,
            'error': reason, 'single_process_order': True,
        }, indent=2))
raise SystemExit(code)
