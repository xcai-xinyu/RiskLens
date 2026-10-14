"""Fixed deterministic regression, with only implemented P0/P1/P2/P3 cases activated."""
import hashlib
import json
import subprocess
import sys
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
raise SystemExit(subprocess.run([sys.executable,'-m','pytest','-q',*selectors,*sys.argv[1:]],cwd=ROOT,
                               check=False,timeout=300).returncode)
