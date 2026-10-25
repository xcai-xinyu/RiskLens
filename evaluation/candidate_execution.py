"""Runtime-only loading, durable output sealing, then independent Gold evaluation.

prepare_runtime_manifest is a separate offline preflight step, never called by runtime.
Legacy manifests remain replayable; formal v3 runs require the explicit preflight artifact.
"""
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from evaluation.candidate_data import (
    CANDIDATE_VERSIONS,
    REQUEST_FIELDS,
    RawSource,
    RuntimeJob,
    file_hash,
    fingerprint,
    jsonlines,
    request_only,
)

ROOT = Path(__file__).resolve().parents[1]


def _write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _path(root, name):
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('runtime_path_escape')
    # A hash check must not become a covert read of a symlinked Gold/review file.
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError('runtime_symlink_forbidden')
    return path


def _legacy_header(directory, root):
    manifest_path = _path(directory, 'manifest.json')
    manifest = json.loads(manifest_path.read_text())
    if CANDIDATE_VERSIONS.get(manifest['data_version']) != manifest['scoring_version']:
        raise ValueError('candidate_version_mismatch')
    allowed = ('inputs/requests.jsonl', 'inputs/source_catalog.json',
               'reviewer_only/annotations.jsonl', 'reviewer_only/evidence.jsonl')
    if not set(allowed) <= set(manifest['files']):
        raise ValueError('missing_runtime_attestation')
    return {'schema_version': 'runtime-input-v1', 'data_version': manifest['data_version'],
            'dataset_directory': str(directory.resolve().relative_to(root.resolve())),
            'dataset_manifest_sha256': file_hash(manifest_path),
            'files': {n: manifest['files'][n] for n in allowed},
            'scoring_version': manifest['scoring_version'], 'preflight': 'LEGACY_HASH_BINDING',
            'protocol': None}


def prepare_runtime_manifest(dataset_directory, destination, *, project_root=None, protocol=None,
                             freeze_version=None):
    """Explicit offline step allowed to read Gold. Emits only allowlisted metadata/hashes."""
    from evaluation.candidate_data import load_candidate_dataset
    from evaluation.contract_scoring import VERSION
    root = Path(project_root or ROOT)
    dataset = Path(dataset_directory)
    data = load_candidate_dataset(dataset, project_root=root)
    header = _legacy_header(dataset, root)
    header.update(preflight='INTEGRITY_CHECKED', scoring_version=VERSION, freeze_version=freeze_version,
                  cases={cid: {'company_group': c['company_group'], 'split': c['split']}
                         for cid, c in data.cases.items()},
                  preflight_checks={'case_count': len(data.cases), 'schema': True,
                                    'source_hashes': True, 'quote_locations': True, 'split': True})
    if protocol is not None:
        path = Path(protocol)
        header['protocol'] = {'path': str(path.resolve().relative_to(root.resolve())), 'sha256': file_hash(path)}
    # This digest detects accidental mutation, not an assertion of external cryptographic trust.
    header['preflight_fingerprint'] = fingerprint(header)
    _write(destination, header)
    return header


def load_runtime_input(directory, case_id, *, project_root=None):
    root = Path(project_root or ROOT)
    directory = Path(directory)
    path = _path(directory, 'runtime_manifest.json')
    if path.exists():
        from evaluation.contract_scoring import VERSION
        header = json.loads(path.read_text())
        expected = header.get('preflight_fingerprint')
        if expected != fingerprint({k: v for k, v in header.items() if k != 'preflight_fingerprint'}):
            raise ValueError('preflight_fingerprint_mismatch')
        allowed = {'schema_version', 'data_version', 'dataset_directory', 'dataset_manifest_sha256',
                   'files', 'scoring_version', 'preflight', 'protocol', 'cases',
                   'preflight_checks', 'preflight_fingerprint', 'freeze_version'}
        if set(header) != allowed or header['preflight'] != 'INTEGRITY_CHECKED':
            raise ValueError('invalid_runtime_manifest')
        if header['scoring_version'] != VERSION:
            raise ValueError('runtime_scoring_version_mismatch')
        if set(header['files']) != {'inputs/requests.jsonl', 'inputs/source_catalog.json',
                                   'reviewer_only/annotations.jsonl', 'reviewer_only/evidence.jsonl'}:
            raise ValueError('runtime_manifest_extra_files')
        dataset = _path(root, header['dataset_directory'])
    else:
        dataset = directory
        header = _legacy_header(dataset, root)
    if file_hash(_path(dataset, 'manifest.json')) != header['dataset_manifest_sha256']:
        raise ValueError('dataset_manifest_changed')
    declared = json.loads(_path(dataset, 'manifest.json').read_text())
    if header['data_version'] != declared['data_version'] or any(
            digest != declared['files'].get(name) for name, digest in header['files'].items()):
        raise ValueError('runtime_dataset_binding_mismatch')
    for name in ('inputs/requests.jsonl', 'inputs/source_catalog.json'):
        if file_hash(_path(dataset, name)) != header['files'][name]:
            raise ValueError('runtime_input_hash_mismatch')
    requests = jsonlines(_path(dataset, 'inputs/requests.jsonl'))
    source_rows = json.loads(_path(dataset, 'inputs/source_catalog.json').read_text())
    sources = []
    for row in source_rows:
        item = dict(row)
        for key in ('raw_path', 'parsed_path'):
            item[key] = _path(root, item[key])
        source = RawSource(**item)
        source.verify()
        date.fromisoformat(source.published_at)
        sources.append(source)
    by_source = {s.document_id: s for s in sources}
    if len(by_source) != len(sources) or len({r['case_id'] for r in requests}) != len(requests):
        raise ValueError('duplicate_runtime_ids')
    split_keys = {'development': set(), 'heldout': set()}
    jobs = {}
    for row in requests:
        if set(row) != {'case_id', 'request', 'document_ids'} or set(row['request']) != REQUEST_FIELDS:
            raise ValueError('request_evaluator_fields')
        request = request_only(row['request'])
        if len(row['document_ids']) != len(set(row['document_ids'])) or not set(row['document_ids']) <= set(by_source):
            raise ValueError('invalid_runtime_source_ids')
        meta = header.get('cases', {}).get(row['case_id'])
        if header['preflight'] == 'INTEGRITY_CHECKED' and (meta is None or set(meta) != {'company_group', 'split'}):
            raise ValueError('runtime_case_metadata')
        split = meta['split'] if meta else ('heldout' if row['case_id'].startswith('v3-heldout-') else 'development')
        selected = []
        for sid in row['document_ids']:
            s = by_source[sid]
            if s.company != request.company or (meta and s.company_group != meta['company_group']):
                raise ValueError('source_company_mismatch')
            split_keys[split].update([s.company_group, s.source_group_id, s.raw_sha256, s.parsed_sha256])
            if s.published_at <= request.as_of_date.isoformat():
                selected.append(s)
        jobs[row['case_id']] = RuntimeJob(row['case_id'], request, tuple(selected))
    if split_keys['development'] & split_keys['heldout']:
        raise ValueError('company_document_or_copy_split_leakage')
    if case_id not in jobs:
        raise ValueError('runtime_case_missing')
    # Headers contain no Gold contents. Only their byte hashes are carried into sealing.
    return jobs[case_id], header


def seal_output(result, header, *, artifact_dir=None, event_sink=None):
    directory = Path(artifact_dir) if artifact_dir else Path(tempfile.mkdtemp(prefix='risklens-sealed-'))
    if artifact_dir:
        directory.mkdir(parents=True, exist_ok=False)
    result['run_id'] = directory.name
    payload = {'result': result, 'runtime_manifest': header, 'runtime_manifest_hash': fingerprint(header)}
    _write(directory / 'runtime-output.json', payload)
    seal = {'schema_version': 'output-seal-v1', 'case_id': result['case_id'], 'run_id': result['run_id'],
            'sealed_at': datetime.now(UTC).isoformat(),
            'files': {'runtime-output.json': file_hash(directory / 'runtime-output.json')},
            'output_hash': fingerprint(result),
            'runtime_evidence_hashes': {r['arm']: fingerprint(r['runtime_evidence']) for r in result['records']},
            'draft_hash': fingerprint(result.get('shared_snapshot', {}).get('draft', [])),
            'source_hash': fingerprint(result.get('source_provenance', [])),
            'runtime_manifest_hash': fingerprint(header)}
    _write(directory / 'OUTPUT_SEAL.json', seal)
    event = {'event': 'output_sealed', 'at': seal['sealed_at'],
             'seal_sha256': file_hash(directory / 'OUTPUT_SEAL.json'), 'directory': str(directory)}
    if event_sink:
        event_sink(event)
    return directory


def evaluate_sealed(directory, *, project_root=None, reviews=None, event_sink=None):
    """No Gold access until both output bytes and binding hashes verify against the seal."""
    from evaluation.candidate_data import load_candidate_dataset
    from evaluation.candidate_scoring import completeness_before_after
    from evaluation.contract_scoring import VERSION
    from evaluation.scorers import aggregate, score
    root, directory = Path(project_root or ROOT), Path(directory)
    seal = json.loads((directory / 'OUTPUT_SEAL.json').read_text())
    if seal.get('schema_version') != 'output-seal-v1' or set(seal['files']) != {'runtime-output.json'}:
        raise ValueError('invalid_output_seal')
    if file_hash(directory / 'runtime-output.json') != seal['files']['runtime-output.json']:
        raise ValueError('output_seal_mismatch')
    payload = json.loads((directory / 'runtime-output.json').read_text())
    result, header = payload['result'], payload['runtime_manifest']
    if (fingerprint(result) != seal['output_hash'] or fingerprint(header) != seal['runtime_manifest_hash']
            or payload['runtime_manifest_hash'] != seal['runtime_manifest_hash']
            or result['case_id'] != seal['case_id'] or result['run_id'] != seal['run_id']
            or {r['arm']: fingerprint(r['runtime_evidence']) for r in result['records']} != seal['runtime_evidence_hashes']
            or fingerprint(result.get('shared_snapshot', {}).get('draft', [])) != seal['draft_hash']
            or fingerprint(result.get('source_provenance', [])) != seal['source_hash']):
        raise ValueError('sealed_binding_mismatch')
    dataset_path = _path(root, header['dataset_directory'])
    if file_hash(dataset_path / 'manifest.json') != header['dataset_manifest_sha256']:
        raise ValueError('evaluation_dataset_changed')
    if header.get('protocol') and file_hash(_path(root, header['protocol']['path'])) != header['protocol']['sha256']:
        raise ValueError('evaluation_protocol_changed')
    for name in ('reviewer_only/annotations.jsonl', 'reviewer_only/evidence.jsonl'):
        if event_sink:
            event_sink({'event': 'evaluator_gold_access', 'path': name, 'at': datetime.now(UTC).isoformat()})
        if file_hash(_path(dataset_path, name)) != header['files'][name]:
            raise ValueError('evaluation_gold_changed')
    dataset = load_candidate_dataset(dataset_path, project_root=root)
    if dataset.manifest['data_version'] != header['data_version']:
        raise ValueError('evaluation_data_version_mismatch')
    version = header['scoring_version']
    if version not in {VERSION, dataset.manifest['scoring_version']}:
        raise ValueError('evaluation_scoring_version_mismatch')
    case = dataset.cases[result['case_id']]
    for record in result['records']:
        options = {'scoring_version': VERSION, 'context': {'run_id': result['run_id'],
                   'case_id': result['case_id'], 'arm': record['arm']}} if version == VERSION else {}
        record['score'] = score(case, record['output'], record['runtime_evidence'],
                               human_review=(reviews or {}).get(record['arm']), **options)
    result['aggregate'] = aggregate(result['records'])
    if version in {VERSION, 'p4-point-review-v2'}:
        before = next(r for r in result['records'] if r['arm'] == 'B')
        if before['shared_snapshot_hash']:
            result['completeness_before_after'] = completeness_before_after(result['records'])
        else:
            result['completeness_before_after'] = [{'before_arm': 'B', 'after_arm': r['arm'],
                'comparison_status': 'UNAVAILABLE_UPSTREAM_FAILURE',
                'before_status': before['score']['status'], 'after_status': r['score']['status'],
                'before': before['score'].get('coverage'), 'after': r['score'].get('coverage')}
                for r in result['records'] if r['arm'] in {'C', 'D', 'C_REPAIR'}]
        for pair in result['completeness_before_after']:
            after = next(r for r in result['records'] if r['arm'] == pair['after_arm'])
            b_claims = before['output']['claims']
            a_claims = after['output']['claims']
            pair.update(before_claims=b_claims, after_claims=a_claims,
                        removed_claim_ids=sorted({c['claim_id'] for c in b_claims} -
                                                 {c['claim_id'] for c in a_claims}))
    result.update(data_version=header['data_version'], freeze_version=header.get('freeze_version'),
                  scoring_version=version, h4_approved=False,
                  sealed_output_directory=str(directory), output_seal_sha256=file_hash(directory / 'OUTPUT_SEAL.json'))
    return result


def main():
    """Explicit offline processes: preflight or post-seal human scoring; never model calls."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    pre = commands.add_parser('preflight')
    pre.add_argument('--dataset', required=True)
    pre.add_argument('--output', required=True)
    pre.add_argument('--protocol', required=True)
    post = commands.add_parser('score-sealed')
    post.add_argument('--run', required=True)
    post.add_argument('--output', required=True)
    post.add_argument('--reviews')
    args = parser.parse_args()
    if args.command == 'preflight':
        prepare_runtime_manifest(args.dataset, args.output, protocol=args.protocol)
    else:
        result = evaluate_sealed(args.run)
        if args.reviews:
            reviews = json.loads(Path(args.reviews).read_text())
            result = evaluate_sealed(args.run, reviews=reviews)
        _write(args.output, result)
    print(json.dumps({'command': args.command, 'real_model_calls': 0, 'h4_approved': False}))


if __name__ == '__main__':
    main()
