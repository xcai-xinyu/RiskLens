"""Explicit unpaid controller preflight; this process may hash frozen Gold files.

Never import or invoke this function from the answering worker. It returns only
integrity metadata and keeps annotations out of the worker's request protocol.
"""
import json
from pathlib import Path

from evaluation.candidate_data import file_hash
from evaluation.candidate_execution import _path


def check_freeze(root, directory):
    root, directory = Path(root), Path(directory)
    manifest = json.loads((directory / 'FREEZE_MANIFEST.json').read_text())
    if (manifest.get('status') != 'DATASET_FROZEN' or not manifest.get('frozen') or
            manifest['scoring_version'] != 'p4-point-review-v3' or manifest['data_count'] != 30):
        raise ValueError('freeze_not_ready')
    for name, digest in manifest['referenced_files'].items():
        if file_hash(_path(root, name)) != digest:
            raise ValueError('frozen_reference_changed')
    for name, digest in manifest['package_files'].items():
        if file_hash(_path(directory, name)) != digest:
            raise ValueError('freeze_package_changed')
    return {'freeze_version': manifest['freeze_version'],
            'freeze_sha256': file_hash(directory / 'FREEZE_MANIFEST.json'),
            'scoring_version': manifest['scoring_version'],
            'scoring_sha256': manifest['scoring_implementation_sha256'],
            'files_checked': len(manifest['referenced_files']) + len(manifest['package_files'])}
