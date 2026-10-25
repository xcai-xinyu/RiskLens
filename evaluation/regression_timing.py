"""Observation-only pytest hooks; no changes to tests, deadlines or outcomes."""
import json
import os
from pathlib import Path
from time import monotonic

START = monotonic()
DATA = {'plugin_started': START, 'tests': [], 'collected_ids': []}


def pytest_sessionstart(session):
    DATA['session_start'] = monotonic()


def pytest_collection(session):
    DATA['collection_start'] = monotonic()


def pytest_collection_finish(session):
    DATA['collection_finish'] = monotonic()
    DATA['collected_ids'] = [item.nodeid for item in session.items]
    save()


def pytest_runtest_logreport(report):
    DATA['tests'].append({'nodeid': report.nodeid, 'phase': report.when,
                          'duration': report.duration, 'outcome': report.outcome})
    DATA['last_test_report'] = monotonic()
    save()


def pytest_sessionfinish(session, exitstatus):
    DATA['session_finish'] = monotonic()
    DATA['exit_code'] = int(exitstatus)
    save()


def pytest_unconfigure(config):
    DATA['unconfigure'] = monotonic()
    save()


def save():
    destination = os.environ.get('RISKLENS_REGRESSION_TIMING')
    if destination:
        Path(destination).write_text(json.dumps(DATA, indent=2))
