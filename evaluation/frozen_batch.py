"""Runtime assembly and independent, all-outputs-first evaluation of a frozen batch."""
import json
from pathlib import Path

from evaluation.candidate_data import file_hash, fingerprint
from evaluation.candidate_execution import _path, _write, load_runtime_input, seal_output
from evaluation.frozen_isolation import runtime_files


def runtime_allowlist(root, frozen):
    """Reads only runtime metadata; never annotations, evidence labels or review files."""
    root, frozen = Path(root), Path(frozen)
    header = json.loads((frozen / 'runtime_manifest.json').read_text())
    dataset = _path(root, header['dataset_directory'])
    paths = [frozen / 'runtime_manifest.json', dataset / 'manifest.json',
             dataset / 'inputs/requests.jsonl', dataset / 'inputs/source_catalog.json']
    catalog = json.loads(paths[-1].read_text())
    # RawSource.verify additionally rejects review paths and incorrect hashes.
    paths += [_path(root, row[key]) for row in catalog for key in ('raw_path', 'parsed_path')]
    paths += list((root / 'app/prompts').glob('*.txt'))
    paths += [root / 'evaluation/prompts/raw_p4_v1.txt',
              root / 'app/configuration/verification_p2_v1.json']
    return paths


async def execute_case(frozen, case_id, *, root, batch_directory, cache_directory,
                       adapter, embedding, ledger, paid_permit=None, context=None):
    """No candidate loader, evaluator or annotation object is accepted here."""
    allowed = runtime_allowlist(root, frozen)
    directory = Path(batch_directory)
    directory.mkdir(parents=True, exist_ok=True)
    with runtime_files(root, allowed, (directory, cache_directory)):
        from evaluation.frozen_runner import run_frozen_job
        job, header = load_runtime_input(frozen, case_id, project_root=root)
        result = await run_frozen_job(job, adapter=adapter, embedding=embedding, ledger=ledger,
            directory=directory / (case_id + '-stages'), index_directory=cache_directory,
            paid_permit=paid_permit, context=context)
        sealed = seal_output(result, header, artifact_dir=directory / case_id)
    return result, sealed


def validate_seal(directory):
    directory = Path(directory)
    seal = json.loads((directory / 'OUTPUT_SEAL.json').read_text())
    path = directory / 'runtime-output.json'
    if (seal.get('schema_version') != 'output-seal-v1' or
            seal.get('files') != {'runtime-output.json': file_hash(path)}):
        raise ValueError('batch_output_tampered')
    payload = json.loads(path.read_text())
    result, header = payload['result'], payload['runtime_manifest']
    from evaluation.frozen_runner import validate_shared
    validate_shared(result)
    if (seal['output_hash'] != fingerprint(result) or
            seal['runtime_manifest_hash'] != fingerprint(header) or
            payload['runtime_manifest_hash'] != fingerprint(header) or
            result['case_id'] != seal['case_id'] or result['run_id'] != seal['run_id'] or
            seal['runtime_evidence_hashes'] != {r['arm']: fingerprint(r['runtime_evidence'])
                                               for r in result['records']} or
            seal['draft_hash'] != fingerprint(result.get('shared_snapshot', {}).get('draft', [])) or
            seal['source_hash'] != fingerprint(result.get('source_provenance', []))):
        raise ValueError('batch_binding_tampered')
    return seal


def seal_batch(directory, case_ids):
    directory = Path(directory)
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError('batch_case_ids')
    authorization = directory / 'authorization.json'
    if authorization.exists() and json.loads(authorization.read_text())['case_ids'] != case_ids:
        raise ValueError('batch_scope_incomplete')
    seals, cohorts = {}, set()
    for cid in case_ids:
        path = _path(directory, cid)
        if validate_seal(path)['case_id'] != cid:
            raise ValueError('batch_case_mismatch')
        seals[cid] = file_hash(path / 'OUTPUT_SEAL.json')
        payload = json.loads((path / 'runtime-output.json').read_text())
        cohorts.add(payload['result'].get('context_execution', {}).get(
            'configuration_hash', 'legacy-frozen-real-v1'))
    if len(cohorts) != 1:
        raise ValueError('COMPARISON_INVALID_CONTEXT_CONFIGURATION')
    _write(directory / 'BATCH_SEAL.json', {'case_ids': case_ids, 'seals': seals})


def evaluate_batch(directory, *, project_root):
    """Every declared case seal is checked before the first evaluation/Gold open."""
    directory = Path(directory)
    batch = json.loads((directory / 'BATCH_SEAL.json').read_text())
    authorization = directory / 'authorization.json'
    if authorization.exists() and json.loads(authorization.read_text())['case_ids'] != batch['case_ids']:
        raise ValueError('batch_scope_incomplete')
    if len(batch['case_ids']) != len(set(batch['case_ids'])) or set(batch['case_ids']) != set(batch['seals']):
        raise ValueError('batch_seal_scope')
    cohorts = set()
    for cid in batch['case_ids']:
        path = _path(directory, cid)
        validate_seal(path)
        if file_hash(path / 'OUTPUT_SEAL.json') != batch['seals'][cid]:
            raise ValueError('batch_seal_changed')
        payload = json.loads((path / 'runtime-output.json').read_text())
        cohorts.add(payload['result'].get('context_execution', {}).get(
            'configuration_hash', 'legacy-frozen-real-v1'))
    if len(cohorts) != 1:
        raise ValueError('COMPARISON_INVALID_CONTEXT_CONFIGURATION')
    # Lazy import is deliberate: the answering process never evaluates Gold.
    from evaluation.candidate_execution import evaluate_sealed
    results = [evaluate_sealed(directory / cid, project_root=project_root) for cid in batch['case_ids']]
    _write(directory / 'EVALUATOR_OUTPUT.json', results)
    return results
