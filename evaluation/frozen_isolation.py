"""Runtime data-file allowlist, including Python import-time and cache reads.

This is an application guard, not an OS sandbox for hostile Python. Models have
no Python execution tool. Production entry installs it before runtime imports.
"""
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

_active = None


def _audit(event, args):
    if event != 'open' or _active is None or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    root, allowed, cache = _active
    path = Path(os.fsdecode(args[0])).absolute()
    # Resolve aliases once; the approved cache is a fresh, owned directory.
    path = path.resolve()
    lower = path.as_posix().lower()
    forbidden = ('/reviewer_only/', '/evaluation/review/', '/evaluation/proposals/')
    if any(part in lower for part in forbidden) or path.name in {
            'CASE_REVIEW.md', 'QUESTIONS.md', 'COVERAGE.csv', '.env'}:
        raise PermissionError('CRITICAL_DATA_LEAKAGE')
    if path in allowed:
        return
    if any(path.is_relative_to(c) for c in cache):
        # Only the documented artifacts/index formats; arbitrary cached annotations
        # are not admitted just because a caller puts them in the cache directory.
        names = {'INDEX_SEAL.json', 'runtime-output.json', 'OUTPUT_SEAL.json',
                 'A.json', 'B.json', 'C.json', 'D.json', 'result.json', 'planner.json',
                 'research.json', 'metrics_node.json', 'analyst.json', 'verifier.json',
                 'B_INITIAL_SNAPSHOT.json', 'budget_ledger.jsonl', 'WHOLE_CASE_PREFLIGHT.json'}
        if (path.name in names or re.fullmatch(r'[a-f0-9]{64}\.(json|txt|pdf)', path.name)
                or path.suffix in {'.sqlite', '.sqlite3', '.bin'}):
            return
        raise PermissionError('runtime_cache_not_allowlisted')
    if path.is_relative_to(root):
        # Code imports are allowed; dataset caches (.json/.txt/.pickle/etc) are not.
        if path.suffix in {'.py', '.pyc', '.so'}:
            return
        if path.is_relative_to(root / '.venv'):
            return
        raise PermissionError('runtime_file_not_allowlisted')


sys.addaudithook(_audit)


@contextmanager
def runtime_files(root, allowed, cache=()):
    global _active
    if _active is not None:
        raise RuntimeError('nested_runtime_guard')
    _active = (Path(root).resolve(), {Path(p).resolve() for p in allowed},
               [Path(p).resolve() for p in cache])
    try:
        yield
    finally:
        _active = None
