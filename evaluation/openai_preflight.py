"""Controller-only code migration check; never relax frozen dataset/source hashes."""
import json
from pathlib import Path

from evaluation.candidate_data import file_hash
from evaluation.candidate_execution import _path

AUTHORIZED_CODE = {'app/adapter.py', 'app/p1_adapter.py', 'app/providers.py',
                   'evaluation/provider_p2.py', 'evaluation/runner.py'}
PLANNER_CODE = {'app/p1_workflow.py', 'app/p2_adapter.py', 'app/p1_service.py'}
OFFLINE_CODE = {'evaluation/run_regression.py'}


def check_openai_freeze(root, directory, migration):
    root, directory = Path(root), Path(directory)
    frozen = json.loads((directory/'FREEZE_MANIFEST.json').read_text())
    if (set(migration) not in (AUTHORIZED_CODE, AUTHORIZED_CODE | PLANNER_CODE,
                              AUTHORIZED_CODE | PLANNER_CODE | OFFLINE_CODE)
            or frozen.get('status') != 'DATASET_FROZEN'
            or not frozen.get('frozen') or frozen['scoring_version'] != 'p4-point-review-v3'
            or frozen['data_count'] != 30):
        raise ValueError('freeze_migration_not_ready')
    for name, digest in frozen['referenced_files'].items():
        current = file_hash(_path(root, name))
        if name in migration:
            row = migration[name]
            historical = row['historical_path']
            attempt = ('attempt-046' if name in OFFLINE_CODE else
                       'attempt-038' if name in PLANNER_CODE else 'attempt-036')
            if (historical != f'evaluation/audit/P4/{attempt}/before/' + name
                    or row['old_sha256'] != digest or file_hash(_path(root, historical)) != digest
                    or row['new_sha256'] != current):
                raise ValueError('frozen_code_migration_drift')
        elif current != digest:
            raise ValueError('frozen_reference_changed')
    for name, digest in frozen['package_files'].items():
        if file_hash(_path(directory, name)) != digest:
            raise ValueError('freeze_package_changed')
    return {'freeze_version': frozen['freeze_version'],
        'freeze_sha256': file_hash(directory/'FREEZE_MANIFEST.json'),
        'scoring_version': frozen['scoring_version'],
        'scoring_sha256': frozen['scoring_implementation_sha256'],
        'files_checked': len(frozen['referenced_files']) + len(frozen['package_files']),
        'data_source_gold_scoring_unchanged': True,
        'authorized_runtime_code_migrations': sorted(migration),
        'original_manifest_rewritten': False}
