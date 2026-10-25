"""p4-point-review-v3: contract units plus independent, output-bound human judgments.

Gold bytes remain v2; the evaluator's manifest selects this scoring overlay explicitly.
Legacy point scoring and Decimal validation are reused, never re-labelled as citations.
"""
import copy
from collections import Counter

from evaluation import candidate_scoring as points
from evaluation.candidate_data import FINALIZATION_SCORING_VERSION, fingerprint

VERSION = 'p4-point-review-v3'
SUPPORT = {'supported', 'partially_supported', 'unsupported', 'contradicted', 'unverifiable', 'pending'}
METRICS = ('citation_coverage', 'citation_correctness', 'unsupported_claim_rate',
           'numerical_accuracy', 'abstention_accuracy', 'false_abstention')


def rate(numerator, denominator, pending=0, applicable=None, failed=0):
    applicable = denominator + pending if applicable is None else applicable
    observed = numerator / denominator if denominator else None
    status = ('PROVISIONAL' if denominator else 'PENDING_HUMAN_REVIEW') if pending else (
        'MEASURED' if denominator else 'N/A')
    if failed:
        status = 'PROVISIONAL' if denominator else 'FAILED_OUTPUT'
    return {'numerator': numerator, 'denominator': denominator, 'reviewed_count': denominator,
            'pending_count': pending, 'n_applicable': applicable, 'n_failed': failed,
            'value': observed if not pending and not failed else None,
            'provisional_value': observed, 'status': status,
            'review_coverage': denominator / applicable if applicable else None}


def _relations(output, evidence):
    rows = []
    metrics = {m['metric_id']: m for m in output.get('metrics', [])}
    for c in output.get('claims', []):
        ids = list(c.get('evidence_ids', []))
        for mid in set(c.get('metric_ids', [])):
            if mid in metrics:
                ids.extend(i['evidence_id'] for i in metrics[mid]['inputs'])
        for eid, count in Counter(ids).items():
            rows.append({'claim_id': c['claim_id'], 'evidence_id': eid,
                         'resolvable': eid in evidence and bool(evidence[eid].get('quote')),
                         'occurrences': count, 'duplicates_removed': count - 1})
    return rows


def review_template(case, output, evidence, *, context):
    if case['scoring_version'] != FINALIZATION_SCORING_VERSION:
        raise ValueError('v3_requires_v2_gold_overlay')
    if set(context) != {'run_id', 'case_id', 'arm'} or context['case_id'] != case['case_id'] or not all(context.values()):
        raise ValueError('review_context_required')
    base = points.review_template(case, output, evidence)
    pending = {'status': 'pending', 'reason': '', 'answer_spans': []}
    return {'scoring_version': VERSION, 'context': dict(context),
            'case_hash': fingerprint(case), 'output_hash': fingerprint(output),
            'runtime_evidence_hash': fingerprint(evidence), 'origin': 'human',
            'reviewer': None, 'reviewed_at': None, 'point_review': base,
            'citations': [{**r, **pending, 'evidence_role': '', 'joint_support_note': ''}
                          for r in _relations(output, evidence)],
            'claim_support': [{'claim_id': c['claim_id'], **pending} for c in output.get('claims', [])],
            'calculations': [{'metric_id': m['metric_id'], 'source_check': 'pending',
                              'definition_review': {'status': 'pending', 'selected_metric_id': m['metric_id'],
                                  'reason': '', 'basis_explanation': 'pending', 'basis_answer_spans': [], 'basis_reason': ''},
                              'reason': ''} for m in output.get('metrics', [])],
            'abstention_review': {'whole_question_refused': None, 'gap_handling': 'pending',
                                   'reason': '', 'answer_spans': []}}


def _ledger(case, output, evidence, review, context):
    template = review_template(case, output, evidence, context=context)
    if review is None:
        return template
    for key in ('scoring_version', 'context', 'case_hash', 'output_hash', 'runtime_evidence_hash', 'origin'):
        if review.get(key) != template[key]:
            raise ValueError('stale_contract_review:' + key)
    if not review.get('reviewer') or not review.get('reviewed_at'):
        raise ValueError('human_review_attribution_required')
    result = copy.deepcopy(review)
    text = points.answer_text(output)
    for field, keys in [('citations', ('claim_id', 'evidence_id')), ('claim_support', ('claim_id',)),
                        ('calculations', ('metric_id',))]:
        expected = {tuple(r[k] for k in keys) for r in template[field]}
        supplied = [tuple(r[k] for k in keys) for r in result[field]]
        if set(supplied) != expected or len(supplied) != len(expected):
            raise ValueError('review_relation_set_mismatch')
        for row in result[field]:
            if any(not s or s not in text for s in row.get('answer_spans', [])):
                raise ValueError('answer_span_not_in_output')
            if field != 'calculations':
                if row['status'] not in SUPPORT:
                    raise ValueError('support_review_enum')
                if row['status'] != 'pending' and not row.get('reason'):
                    raise ValueError('support_reason_required')
                if field == 'citations':
                    actual = next(r for r in template[field] if all(r[k] == row[k] for k in keys))
                    for k in ('resolvable', 'occurrences', 'duplicates_removed'):
                        if row[k] != actual[k]:
                            raise ValueError('relation_diagnostics_changed')
                    if row['status'] in {'supported', 'partially_supported'} and (
                            not actual['resolvable'] or not row.get('evidence_role')):
                        raise ValueError('support_requires_resolved_citation_and_role')
            else:
                definition = row['definition_review']
                if row['source_check'] not in {'supported', 'unsupported', 'pending'} or definition['status'] not in {'accepted', 'rejected', 'pending'}:
                    raise ValueError('calculation_review_enum')
                if row['source_check'] != 'pending' and not row.get('reason'):
                    raise ValueError('source_reason_required')
                if definition['selected_metric_id'] != row['metric_id'] or (
                        definition['status'] != 'pending' and not definition.get('reason')):
                    raise ValueError('definition_review_binding')
    a = result['abstention_review']
    if a['whole_question_refused'] not in (None, True, False) or a['gap_handling'] not in {'correct', 'incorrect', 'pending', 'not_applicable'}:
        raise ValueError('abstention_review_enum')
    if (a['whole_question_refused'] is not None or a['gap_handling'] != 'pending') and not a['reason']:
        raise ValueError('abstention_reason_required')
    if any(not s or s not in text for s in a.get('answer_spans', [])):
        raise ValueError('answer_span_not_in_output')
    # A single attributed record binds all decisions; reuse v2's exact point validators.
    result['point_review'].update(reviewer=review['reviewer'], reviewed_at=review['reviewed_at'])
    return result


def _support_rate(rows, positive):
    counts = {s: sum(r['status'] == s for r in rows) for s in sorted(SUPPORT)}
    return {**rate(sum(counts[s] for s in positive), len(rows)-counts['pending'], counts['pending']),
            'state_counts': counts}


def _numeric(case, output, evidence, ledger):
    rows = []
    for m in output.get('metrics', []):
        group = next((g for g in case['metric_groups'] if g['name'] == m['name']), None)
        check = points.numeric_check(m, group['references'] if group else [], evidence)
        human = next(r for r in ledger['calculations'] if r['metric_id'] == m['metric_id'])
        definition = human['definition_review']
        if group:
            definition = points._definition_review(group, definition, [m], output)
        hard_ok = (check['arithmetic'] == 'PASS' and check['input_ids_resolve']
                   and check['component_structure'] != 'FAIL'
                   and (not group or group['selection'] != 'fixed' or check['reference_input_match']))
        if not hard_ok or human['source_check'] == 'unsupported' or definition['status'] == 'rejected':
            status = 'incorrect'
        elif human['source_check'] == 'pending' or definition['status'] == 'pending':
            status = 'pending'
        else:
            status = 'correct'
        rows.append({'metric_id': m['metric_id'], 'deterministic_check': check,
                     'source_check': human['source_check'], 'definition_review': definition,
                     'final_status': status})
    counts = Counter(r['final_status'] for r in rows)
    missing = [g['group_id'] for g in case['metric_groups'] if g['required']
               and not any(m['name'] == g['name'] for m in output.get('metrics', []))]
    return {**rate(counts['correct'], counts['correct']+counts['incorrect'], counts['pending']),
            'items': rows, 'missing_metric': missing, 'partial_weight': None}


def _abstention(case, ledger):
    whole = case.get('whole_question_abstention_expected', False)
    gaps = bool(case.get('expected_gaps'))
    row = ledger['abstention_review']
    refused = row['whole_question_refused']
    if not whole and not gaps:
        abstain = rate(0, 0)
    elif refused is None or (gaps and not whole and not refused and row['gap_handling'] in {'pending', 'not_applicable'}):
        abstain = rate(0, 0, 1)
    else:
        correct = refused if whole else (not refused and row['gap_handling'] == 'correct')
        abstain = rate(int(correct), 1)
    false = rate(0, 0) if whole else (rate(0, 0, 1) if refused is None else rate(int(refused), 1))
    policy = {'whole_question_abstention_expected': whole, 'local_gaps_present': gaps,
              'human_decision': row, 'gap_does_not_imply_whole_refusal': True}
    return {**abstain, **policy}, {**false, **policy}


def score_contract(case, output, evidence, review=None, *, context):
    ledger = _ledger(case, output, evidence, review, context)
    base = points.score_candidate(case, output, evidence, ledger['point_review'] if review else None)
    relations = _relations(output, evidence)
    facts = [c for c in output.get('claims', []) if c['claim_type'] == 'fact']
    cited = {r['claim_id'] for r in relations if r['resolvable']}
    coverage = {**rate(sum(c['claim_id'] in cited for c in facts), len(facts)),
                'total_fact_claims': len(facts),
                'facts_without_resolvable_citation': [c['claim_id'] for c in facts if c['claim_id'] not in cited],
                'relations': relations,
                'unresolved_metric_references': [
                    {'claim_id': c['claim_id'], 'metric_id': mid}
                    for c in output.get('claims', []) for mid in set(c.get('metric_ids', []))
                    if mid not in {m['metric_id'] for m in output.get('metrics', [])}]}
    abstain, false = _abstention(case, ledger)
    metrics = {'citation_coverage': coverage,
               'citation_correctness': _support_rate(ledger['citations'], {'supported'}),
               'unsupported_claim_rate': _support_rate(ledger['claim_support'], {'unsupported', 'contradicted'}),
               'numerical_accuracy': _numeric(case, output, evidence, ledger),
               'abstention_accuracy': abstain, 'false_abstention': false}
    failed = output.get('status') == 'failed'
    if failed:
        metrics = {k: {**v, **rate(0, 0, applicable=v['n_applicable'], failed=1),
                       'unscored_failure_observations': v} for k, v in metrics.items()}
    return {**base, 'scorer_version': VERSION, 'context': dict(context),
            'metrics': metrics, 'human_record': ledger,
            'status': 'FAILED_OUTPUT' if failed else ('PENDING_HUMAN_REVIEW' if
                base['status'] == 'PENDING_HUMAN_REVIEW' or any(m['pending_count'] for m in metrics.values()) else 'REVIEWED'),
            'output_status': output.get('status'), 'original_gold_scoring_version': case['scoring_version'],
            'claim_set': copy.deepcopy(output.get('claims', [])),
            'verifier_status_used_as_gold': False}


def aggregate_contract(records):
    if any(r['score'].get('scorer_version') != VERSION for r in records):
        raise ValueError('mixed_scorer_versions')
    old = copy.deepcopy(records)
    for row in old:
        row['score']['scorer_version'] = FINALIZATION_SCORING_VERSION
    result = points.aggregate_candidate(old)
    for arm, aggregate in result.items():
        rows = [r['score'] for r in records if r['arm'] == arm]
        aggregate['scorer_version'] = VERSION
        aggregate['per_case'] = [{'context': r['context'], 'metrics': r['metrics'],
                                  'output_status': r['output_status']} for r in rows]
        aggregate['execution_coverage'] = {s: sum(r['output_status'] == s for r in rows)
                                            for s in ('completed', 'partial', 'failed')}
        aggregate['metrics'] = {}
        for name in METRICS:
            values = [r['metrics'][name] for r in rows]
            combined = rate(sum(m['numerator'] for m in values), sum(m['denominator'] for m in values),
                            sum(m['pending_count'] for m in values), sum(m['n_applicable'] for m in values),
                            sum(m['n_failed'] for m in values))
            eligible = [m['value'] for m in values if m['status'] == 'MEASURED' and m['value'] is not None]
            combined.update(primary='macro', secondary='micro',
                primary_value=(sum(eligible)/len(eligible) if eligible else None)
                    if not combined['pending_count'] and not combined['n_failed'] else None,
                macro_value=sum(eligible)/len(eligible) if eligible else None,
                macro_eligible_cases=len(eligible), total_cases=len(rows),
                micro_value=combined['provisional_value'], n_not_applicable=sum(m['status'] == 'N/A' for m in values),
                pending_cases=sum(m['pending_count'] > 0 for m in values),
                partial_weight=None)
            aggregate['metrics'][name] = combined
    return result
