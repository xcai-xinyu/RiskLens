"""Explicit v4 post-seal overlay. Never edits frozen Gold or v3 scores.

Only period equivalence changes. Arithmetic, units, source/definition judgments,
point coverage and pending semantics remain independent.
"""
import copy
import json
from decimal import InvalidOperation
from pathlib import Path

from app.financial_periods import equivalent
from evaluation import candidate_scoring as points
from evaluation import contract_scoring as legacy

VERSION = 'p4-point-review-v4-periods'


def numeric_check(metric, references, evidence, *, company, metadata):
    result = points.numeric_check(metric, references, evidence)
    result['legacy_reference_input_match'] = result['reference_input_match']
    traces = []
    outcomes = []
    for ref in references:
        try:
            a, b = points._inputs(metric), points._inputs(ref)
        except (KeyError, ValueError, InvalidOperation, TypeError):
            outcomes.append('MISMATCH')
            continue
        if a.keys() != b.keys() or any(a[k][:2] != b[k][:2] for k in a):
            outcomes.append('MISMATCH')
            continue
        periods = []
        for item in metric['inputs']:
            e = evidence.get(item['evidence_id'], {})
            trace = equivalent(a[item['name']][2], b[item['name']][2], company=company,
                metric_name=metric['name'], metadata=metadata,
                document_ids={e.get('document_id')})
            periods.append(trace['status'])
            traces.append({'input_name': item['name'], 'reference_metric_id': ref['metric_id'], **trace})
        outcomes.append('MISMATCH' if 'MISMATCH' in periods else
                        'UNKNOWN' if 'UNKNOWN' in periods else 'MATCH')
    status = 'MATCH' if 'MATCH' in outcomes else 'UNKNOWN' if 'UNKNOWN' in outcomes else 'MISMATCH'
    result.update(reference_input_match=status == 'MATCH', reference_match_status=status,
                  period_traces=traces, period_version='source-bound-period-v1')
    return result


def score_contract(case, output, evidence, review=None, *, context, metadata):
    # An existing v3 human decision cannot be silently imported into v4.
    translated = copy.deepcopy(review)
    if translated is not None:
        if translated['scoring_version'] != VERSION:
            raise ValueError('stale_period_review_version')
        translated['scoring_version'] = legacy.VERSION
    result = legacy.score_contract(case, output, evidence, translated, context=context)
    result['scorer_version'] = VERSION
    result['human_record']['scoring_version'] = VERSION
    result['period_metadata'] = copy.deepcopy(metadata)
    result['supersedes_scoring_version'] = legacy.VERSION
    if output.get('status') == 'failed':
        return result
    by_metric = {m['metric_id']: m for m in output.get('metrics', [])}
    groups = {g['group_id']: g for g in case['metric_groups']}
    old_pending = sum(g['status'] == 'PENDING_HUMAN_REVIEW' for g in result['metric_groups'])
    for group in result['metric_groups']:
        definition = groups[group['group_id']]
        group['checks'] = [numeric_check(by_metric[c['metric_id']], definition['references'], evidence,
            company=case['company'], metadata=metadata) for c in group['checks']]
        valid = [c for c in group['checks'] if c['arithmetic'] == 'PASS' and c['input_ids_resolve']
                 and c['component_structure'] != 'FAIL' and
                 (definition['selection'] != 'fixed' or c['reference_input_match'])]
        unknown = any(c['arithmetic'] == 'PASS' and c['input_ids_resolve'] and
            c['component_structure'] != 'FAIL' and c['reference_match_status'] == 'UNKNOWN'
            for c in group['checks'])
        human = group['definition_review']
        if group['checks']:
            group['status'] = ('PENDING_HUMAN_REVIEW' if unknown and not valid else
                'DETERMINISTIC_FAIL' if not valid else
                'PENDING_HUMAN_REVIEW' if human['status'] == 'pending' else
                'HUMAN_REJECTED' if human['status'] == 'rejected' else
                'ACCEPTED_BY_HUMAN_WITH_VALID_ARITHMETIC' if human['selected_metric_id'] in
                    {c['metric_id'] for c in valid} else 'DETERMINISTIC_FAIL')
    numeric = result['metrics']['numerical_accuracy']
    for row in numeric['items']:
        metric = by_metric[row['metric_id']]
        group = next((g for g in groups.values() if g['name'] == metric['name']), None)
        check = numeric_check(metric, group['references'] if group else [], evidence,
                              company=case['company'], metadata=metadata)
        row['deterministic_check'] = check
        hard = (check['arithmetic'] == 'PASS' and check['input_ids_resolve'] and
                check['component_structure'] != 'FAIL')
        fixed = group and group['selection'] == 'fixed'
        human = row['definition_review']
        if not hard or (fixed and check['reference_match_status'] == 'MISMATCH') or row['source_check'] == 'unsupported' or human['status'] == 'rejected':
            row['final_status'] = 'incorrect'
        elif (fixed and check['reference_match_status'] == 'UNKNOWN') or row['source_check'] == 'pending' or human['status'] == 'pending':
            row['final_status'] = 'pending'
        else:
            row['final_status'] = 'correct'
    statuses = [r['final_status'] for r in numeric['items']]
    numeric.update(legacy.rate(statuses.count('correct'), statuses.count('correct') + statuses.count('incorrect'), statuses.count('pending')))
    # Pending is not zero, incorrect, or an automated PASS.
    if any(g['status'] == 'PENDING_HUMAN_REVIEW' for g in result['metric_groups']) or numeric['pending_count']:
        result['status'] = 'PENDING_HUMAN_REVIEW'
    result['pending_assessment_cells'] += sum(g['status'] == 'PENDING_HUMAN_REVIEW'
        for g in result['metric_groups']) - old_pending
    return result


def aggregate(records):
    if any(r['score']['scorer_version'] != VERSION for r in records):
        raise ValueError('mixed_scorer_versions')
    compatible = copy.deepcopy(records)
    for r in compatible:
        r['score']['scorer_version'] = legacy.VERSION
    result = legacy.aggregate_contract(compatible)
    for value in result.values():
        value['scorer_version'] = VERSION
    return result


def evaluate_batch_overlay(directory, *, project_root, metadata_by_case, reviews=None):
    """Production post-seal evaluator; all batch seals checked before Gold access.

    Retains the v3 evaluation in memory as an explicit prior-rule comparison.
    Never writes to an existing sealed output directory.
    """
    from evaluation.candidate_data import file_hash, jsonlines
    from evaluation.candidate_execution import _path, evaluate_sealed
    from evaluation.frozen_batch import validate_seal
    directory, root = Path(directory), Path(project_root)
    batch = json.loads((directory/'BATCH_SEAL.json').read_text())
    if len(batch['case_ids']) != len(set(batch['case_ids'])) or set(batch['case_ids']) != set(batch['seals']):
        raise ValueError('batch_seal_scope')
    for cid in batch['case_ids']:
        validate_seal(_path(directory, cid))
        if file_hash(directory/cid/'OUTPUT_SEAL.json') != batch['seals'][cid]:
            raise ValueError('batch_seal_changed')
    results = []
    for cid in batch['case_ids']:
        legacy_result = evaluate_sealed(directory/cid, project_root=root)
        payload = json.loads((directory/cid/'runtime-output.json').read_text())
        header = payload['runtime_manifest']
        dataset = _path(root, header['dataset_directory'])
        case = next(c for c in jsonlines(dataset/'reviewer_only/annotations.jsonl') if c['case_id'] == cid)
        result = copy.deepcopy(legacy_result)
        result['prior_scoring_version'] = legacy.VERSION
        result['scoring_version'] = VERSION
        for record in result['records']:
            record['prior_score'] = copy.deepcopy(record['score'])
            record['score'] = score_contract(case, record['output'], record['runtime_evidence'],
                review=(reviews or {}).get((cid, record['arm'])), metadata=metadata_by_case[cid],
                context={'run_id': result['run_id'], 'case_id': cid, 'arm': record['arm']})
        result['aggregate'] = aggregate(result['records'])
        # Coverage/answer sets are unchanged by a period-only scorer overlay.
        result['score_overlay_only'] = True
        results.append(result)
    return results
