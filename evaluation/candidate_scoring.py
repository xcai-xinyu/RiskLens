"""Deterministic metric checks and output-bound human semantics, never exact-text Gold."""
from decimal import Decimal, InvalidOperation, localcontext

from app.tools.metrics import UNITS, calculate_metric
from evaluation.candidate_data import (
    FINALIZATION_SCORING_VERSION,
    SCORING_VERSION,
    fingerprint,
    validate_case,
)

COVERAGE = {'complete', 'partial', 'uncovered', 'pending'}
CORRECTNESS = {'correct', 'partially_correct', 'incorrect', 'not_applicable', 'pending'}
SUPPORT = {'supported', 'partial', 'unsupported', 'not_applicable', 'pending'}


def answer_text(output):
    """Only actual answer material, not a model's self-assessment or evaluator notes."""
    parts = [c['text'] for c in output.get('claims', [])]
    parts += [g if isinstance(g, str) else g.get('message', '') for g in output.get('gaps', [])]
    parts += [output.get('summary', ''), output.get('answer', '')]
    return '\n'.join(parts)


def review_template(case, output, evidence):
    validate_case(case)
    for field, identifier in [('claims', 'claim_id'), ('metrics', 'metric_id')]:
        ids = [item[identifier] for item in output.get(field, [])]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate_output_' + identifier)
    template = {'scoring_version': case['scoring_version'], 'case_id': case['case_id'],
            'case_hash': fingerprint(case), 'output_hash': fingerprint(output),
            'runtime_evidence_hash': fingerprint(evidence),
            'reviewer': None, 'reviewed_at': None, 'origin': 'human',
            'criteria': case['obligations'],
            'answer_policy': {'whole_question_abstention_expected': case.get('whole_question_abstention_expected', False),
                              'expected_gaps': case.get('expected_gaps', []),
                              'rule': 'Missing future predictions or one metric input do not automatically justify refusing answerable facts.'},
            'points': [{'point_id': p['point_id'], 'coverage': 'pending',
                        'correctness': 'pending', 'citation_support': 'pending',
                        'answer_spans': [], 'citation_ids': [], 'reason': ''}
                       for p in case['obligations']],
            'metric_definitions': [{'group_id': g['group_id'], 'status': 'pending',
                                    'selected_metric_id': None, 'reason': ''}
                                   for g in case['metric_groups']],
            'claims': [{'claim_id': c['claim_id'], 'correctness': 'pending',
                        'citation_support': 'pending', 'reason': ''} for c in output.get('claims', [])],
            'abstention': {'status': 'pending', 'reason': ''}}
    if case['scoring_version'] == FINALIZATION_SCORING_VERSION:
        for group, row in zip(case['metric_groups'], template['metric_definitions'], strict=True):
            if group.get('definition_policy'):
                row.update(policy=group['definition_policy'], basis_explanation='pending',
                           basis_answer_spans=[], basis_reason='')
    return template


def _review(case, output, review, evidence):
    template = review_template(case, output, evidence)
    if review is None:
        return template
    for key in ('scoring_version', 'case_id', 'case_hash', 'output_hash', 'runtime_evidence_hash', 'origin'):
        if review.get(key) != template[key]:
            raise ValueError('stale_or_invalid_human_review:' + key)
    if not review.get('reviewer') or not review.get('reviewed_at'):
        raise ValueError('human_review_attribution_required')
    points = {p['point_id']: p for p in template['points']}
    seen = set()
    cited = {eid for c in output.get('claims', []) for eid in c.get('evidence_ids', [])}
    cited.update(i['evidence_id'] for m in output.get('metrics', []) for i in m.get('inputs', []))
    text = answer_text(output)
    for p in review.get('points', []):
        pid = p['point_id']
        if pid not in points or pid in seen:
            raise ValueError('unknown_or_duplicate_review_point')
        seen.add(pid)
        if p['coverage'] not in COVERAGE or p['correctness'] not in CORRECTNESS or p['citation_support'] not in SUPPORT:
            raise ValueError('human_review_enum')
        if any(p[k] != 'pending' for k in ('coverage', 'correctness', 'citation_support')) and not p.get('reason'):
            raise ValueError('human_review_reason_required')
        spans = p.get('answer_spans', [])
        if any(not span or span not in text for span in spans):
            raise ValueError('answer_span_not_in_output')
        if p['coverage'] in {'complete', 'partial'} and not spans:
            raise ValueError('positive_coverage_requires_answer_span')
        if p['citation_support'] in {'supported', 'partial'}:
            refs = p.get('citation_ids', [])
            if not refs or not set(refs) <= cited & set(evidence) or not spans:
                raise ValueError('positive_support_requires_actual_resolvable_citation')
        if p['coverage'] == 'uncovered' and p['correctness'] in {'correct', 'partially_correct'}:
            raise ValueError('uncovered_point_cannot_be_correct_content')
        points[pid] = p
    template['points'] = list(points.values())
    groups = {g['group_id']: g for g in template['metric_definitions']}
    seen = set()
    for g in review.get('metric_definitions', []):
        if g['group_id'] not in groups or g['group_id'] in seen or g['status'] not in {'accepted', 'rejected', 'pending'}:
            raise ValueError('invalid_metric_definition_review')
        seen.add(g['group_id'])
        if g['status'] != 'pending' and (not g.get('reason') or not g.get('selected_metric_id')):
            raise ValueError('metric_definition_reason_required')
        groups[g['group_id']] = g
    template['metric_definitions'] = list(groups.values())
    claims = {c['claim_id']: c for c in template['claims']}
    output_claims = {c['claim_id']: c for c in output.get('claims', [])}
    seen = set()
    for c in review.get('claims', []):
        if c['claim_id'] not in claims or c['claim_id'] in seen:
            raise ValueError('invalid_claim_review')
        seen.add(c['claim_id'])
        if c['correctness'] not in CORRECTNESS or c['citation_support'] not in SUPPORT:
            raise ValueError('claim_review_enum')
        if (c['correctness'] != 'pending' or c['citation_support'] != 'pending') and not c.get('reason'):
            raise ValueError('claim_review_reason_required')
        if c['citation_support'] in {'supported', 'partial'}:
            refs = output_claims[c['claim_id']].get('evidence_ids', [])
            if not refs or not set(refs) <= set(evidence):
                raise ValueError('positive_claim_support_requires_resolvable_citation')
        claims[c['claim_id']] = c
    template['claims'] = list(claims.values())
    abstention = review.get('abstention', template['abstention'])
    if abstention['status'] not in {'appropriate', 'unnecessary', 'not_abstained', 'pending'}:
        raise ValueError('abstention_review_enum')
    if abstention['status'] != 'pending' and not abstention.get('reason'):
        raise ValueError('abstention_reason_required')
    template.update(abstention=abstention, reviewer=review['reviewer'], reviewed_at=review['reviewed_at'])
    return template


def _inputs(metric):
    with localcontext() as ctx:
        ctx.prec = 34
        return {i['name']: (Decimal(str(i['value'])) * UNITS[i['unit']], i['currency'], i['period'])
                for i in metric['inputs']}


def _equal_result(actual, expected):
    a, b = Decimal(str(actual)), Decimal(str(expected))
    if not a.is_finite() or not b.is_finite():
        return False
    if a == b:
        return True
    # The reviewed display rule is four decimals or more, not a free tolerance.
    digits = -a.as_tuple().exponent
    with localcontext() as ctx:
        ctx.prec = 34
        return 4 <= digits <= 34 and a == b.quantize(Decimal(1).scaleb(-digits))


def numeric_check(metric, references, evidence):
    try:
        calc = calculate_metric(metric['name'], metric['inputs'])
        formula = ''.join(metric['formula'].split()) == ''.join(calc.formula.split())
        arithmetic = (calc.status == metric['status'] == 'success' and formula
                      and _equal_result(metric['value'], calc.value))
        resolved = all(i['evidence_id'] in evidence for i in metric['inputs'])
        ref_match = any(_inputs(metric) == _inputs(ref) for ref in references)
        components = metric.get('components', [])
        structure = 'NOT_SUPPLIED'
        if components:
            keys = [c['component_id'] for c in components]
            sums = {}
            for c in components:
                parent = next(i for i in metric['inputs'] if i['name'] == c['input_name'])
                if c['currency'] != parent['currency'] or c['period'] != parent['period']:
                    raise ValueError('component_period_currency_mismatch')
                if c['evidence_id'] not in evidence:
                    raise ValueError('component_citation_unresolved')
                sums[c['input_name']] = sums.get(c['input_name'], Decimal(0)) + Decimal(str(c['value'])) * UNITS[c['unit']]
            structure = ('PASS' if len(keys) == len(set(keys)) and all(
                sums[name] == _inputs(metric)[name][0] for name in sums) else 'FAIL')
        return {'metric_id': metric['metric_id'], 'arithmetic': 'PASS' if arithmetic else 'FAIL',
                'input_ids_resolve': resolved, 'reference_input_match': ref_match,
                'component_structure': structure, 'calculated_value': str(calc.value) if calc.value is not None else None,
                'calculation_status': calc.status, 'limitations': calc.limitations,
                'source_entailment': 'PENDING_HUMAN_REVIEW'}
    except (ValueError, KeyError, TypeError, StopIteration, InvalidOperation):
        return {'metric_id': metric.get('metric_id'), 'arithmetic': 'FAIL',
                'input_ids_resolve': False, 'reference_input_match': False,
                'component_structure': 'FAIL', 'source_entailment': 'PENDING_HUMAN_REVIEW'}


def _counts(values, names):
    counts = {name: values.count(name) for name in names}
    counts['total'] = len(values)
    counts['reviewed'] = len(values) - counts.get('pending', 0)
    counts['review_coverage'] = counts['reviewed'] / len(values) if values else None
    return counts


def _definition_review(group, human, matches, output):
    """Arithmetic equality never establishes equivalence of financial definitions."""
    policy = group.get('definition_policy')
    if not policy or human['status'] != 'accepted':
        return human
    selected = next((m for m in matches if m['metric_id'] == human['selected_metric_id']), None)
    standard = next(r for r in group['references']
                    if r['metric_id'] == policy['standard_reference_id'])
    try:
        is_standard = selected is not None and _inputs(selected) == _inputs(standard)
    except (ValueError, KeyError, InvalidOperation):
        is_standard = False
    if is_standard:
        return human  # Still requires the existing explicit source/definition human judgment.
    spans = human.get('basis_answer_spans', [])
    if any(not span or span not in answer_text(output) for span in spans):
        raise ValueError('basis_explanation_span_not_in_output')
    if human.get('basis_explanation', 'pending') not in {'pending', 'adequate', 'inadequate'}:
        raise ValueError('basis_explanation_enum')
    explained = (human.get('basis_explanation') == 'adequate' and spans
                 and human.get('basis_reason'))
    if not explained:
        return {**human, 'status': 'pending', 'requested_status': 'accepted',
                'pending_reason': 'Nonstandard/mixed measurement needs output-bound human review of composition, bases and limits.'}
    return human


def score_candidate(case, output, evidence, review=None):
    validate_case(case)
    if output.get('status') == 'failed':
        return {'scorer_version': case['scoring_version'], 'status': 'FAILED_OUTPUT',
                'case_hash': fingerprint(case), 'output_hash': fingerprint(output),
                'quality': None, 'failure_retained': True}
    ledger = _review(case, output, review, evidence)
    point_map = {p['point_id']: p for p in ledger['points']}
    required = [point_map[p['point_id']] for p in case['obligations'] if p['completeness']]
    coverage = _counts([p['coverage'] for p in required], sorted(COVERAGE))
    coverage['full_coverage_rate_among_reviewed'] = coverage['complete'] / coverage['reviewed'] if coverage['reviewed'] else None
    coverage['partial_weight'] = None
    guard_ids = {p['point_id'] for p in case['obligations'] if p.get('assessment_role') == 'guardrail_only'}
    content = [p for p in ledger['points'] if p['point_id'] not in guard_ids]
    guards = [p for p in ledger['points'] if p['point_id'] in guard_ids]
    correctness = _counts([p['correctness'] for p in content], sorted(CORRECTNESS))
    support = _counts([p['citation_support'] for p in content], sorted(SUPPORT))
    guard_checks = {'correctness': _counts([p['correctness'] for p in guards], sorted(CORRECTNESS)),
                    'citation_support': _counts([p['citation_support'] for p in guards], sorted(SUPPORT)),
                    'earns_duplicate_credit': False}
    metric_results = []
    for group in case['metric_groups']:
        matches = [m for m in output.get('metrics', []) if m.get('name') == group['name']]
        checks = [numeric_check(m, group['references'], evidence) for m in matches]
        human = next(g for g in ledger['metric_definitions'] if g['group_id'] == group['group_id'])
        if human['selected_metric_id'] and human['selected_metric_id'] not in {m['metric_id'] for m in matches}:
            raise ValueError('reviewed_metric_missing_from_output')
        if case['scoring_version'] == FINALIZATION_SCORING_VERSION:
            human = _definition_review(group, human, matches, output)
            # Keep the submitted human record immutable; only the effective result is pending.
            ledger['metric_definitions'] = [human if g['group_id'] == group['group_id'] else g
                                            for g in ledger['metric_definitions']]
        valid = [c for c in checks if c['arithmetic'] == 'PASS' and c['input_ids_resolve']
                 and c['component_structure'] != 'FAIL'
                 and (group['selection'] != 'fixed' or c['reference_input_match'])]
        if not matches:
            status = 'MISSING_OUTPUT'
        elif not valid:
            status = 'DETERMINISTIC_FAIL'
        elif human['status'] == 'pending':
            status = 'PENDING_HUMAN_REVIEW'
        elif human['status'] == 'rejected':
            status = 'HUMAN_REJECTED'
        elif human['selected_metric_id'] not in {c['metric_id'] for c in valid}:
            status = 'DETERMINISTIC_FAIL'
        else:
            status = 'ACCEPTED_BY_HUMAN_WITH_VALID_ARITHMETIC'
        metric_results.append({'group_id': group['group_id'], 'selection': group['selection'],
                               'required': group['required'], 'status': status, 'checks': checks,
                               'definition_review': human})
    claims = ledger['claims']
    claim_counts = _counts([c['correctness'] for c in claims], sorted(CORRECTNESS))
    pending = (coverage['pending'] + correctness['pending'] + support['pending']
               + claim_counts['pending'] + sum(c['citation_support'] == 'pending' for c in claims)
               + sum(m['status'] == 'PENDING_HUMAN_REVIEW' for m in metric_results)
               + guard_checks['correctness']['pending'] + guard_checks['citation_support']['pending']
               + (ledger['abstention']['status'] == 'pending'))
    return {'scorer_version': case['scoring_version'], 'status': 'PENDING_HUMAN_REVIEW' if pending else 'REVIEWED',
            'case_hash': fingerprint(case), 'output_hash': fingerprint(output),
            'runtime_evidence_hash': fingerprint(evidence),
            'coverage': coverage, 'correctness': correctness, 'citation_support': support,
            'guardrail_checks': guard_checks,
            'metric_groups': metric_results, 'claims': claims, 'claim_correctness': claim_counts,
            'abstention': ledger['abstention'], 'points': ledger['points'],
            'pending_assessment_cells': pending,
            'verifier_status_used_as_gold': False, 'human_record': ledger,
            'note': 'REVIEWED means judgments recorded, not all content correct. Partial counts are not assigned an unapproved weight.'}


def aggregate_candidate(records):
    versions = {r['score'].get('scorer_version') for r in records}
    if len(versions) > 1 or versions - {SCORING_VERSION, FINALIZATION_SCORING_VERSION}:
        raise ValueError('mixed_scorer_versions')
    version = next(iter(versions), SCORING_VERSION)
    result = {}
    for arm in sorted({r['arm'] for r in records}):
        rows = [r['score'] for r in records if r['arm'] == arm]
        measured = [r for r in rows if r['status'] != 'FAILED_OUTPUT']
        fields = {}
        for field, values in [('coverage', COVERAGE), ('correctness', CORRECTNESS), ('citation_support', SUPPORT)]:
            counts = {v: sum(r[field][v] for r in measured) for v in sorted(values)}
            counts['total'] = sum(r[field]['total'] for r in measured)
            counts['reviewed'] = counts['total'] - counts['pending']
            counts['review_coverage'] = counts['reviewed'] / counts['total'] if counts['total'] else None
            fields[field] = counts
        result[arm] = {'scorer_version': version, 'cases': len(rows),
                       'failed': len(rows) - len(measured), 'quality': fields,
                       'pending_cases': sum(r['status'] == 'PENDING_HUMAN_REVIEW' for r in rows),
                       'cost': None, 'cost_reason': 'No supported pricing estimate.'}
        statuses = [g['status'] for r in measured for g in r['metric_groups']]
        metric_counts = {s: statuses.count(s) for s in (
            'MISSING_OUTPUT', 'DETERMINISTIC_FAIL', 'PENDING_HUMAN_REVIEW',
            'HUMAN_REJECTED', 'ACCEPTED_BY_HUMAN_WITH_VALID_ARITHMETIC')}
        metric_counts['total'] = len(statuses)
        reviewed = sum(g['definition_review']['status'] != 'pending'
                       for r in measured for g in r['metric_groups'])
        metric_counts.update(definitions_reviewed=reviewed,
                             definitions_pending=len(statuses)-reviewed,
                             definition_review_coverage=reviewed/len(statuses) if statuses else None)
        result[arm]['metric_groups'] = metric_counts
        result[arm]['guardrail_pending_cells'] = sum(
            r['guardrail_checks']['correctness']['pending'] + r['guardrail_checks']['citation_support']['pending']
            for r in measured)
        if version == FINALIZATION_SCORING_VERSION:
            result[arm]['averages'] = _averages(measured, len(rows))
    return result


def _averages(measured, total_cases):
    """No pending-as-zero, no partial weight, and no hidden eligible-case denominator."""
    result = {'primary': 'macro', 'secondary': 'micro', 'partial_weight': None,
              'status': 'PROVISIONAL' if len(measured) < total_cases or any(
                  r['status'] == 'PENDING_HUMAN_REVIEW' for r in measured) else 'REVIEWED',
              'dimensions': {}}
    for field, categories in [('coverage', COVERAGE), ('correctness', CORRECTNESS),
                              ('citation_support', SUPPORT)]:
        applicable = sorted(categories - {'pending', 'not_applicable'})
        rows = [r[field] for r in measured]
        eligible = [r for r in rows if r['pending'] == 0 and sum(r[c] for c in applicable)]
        numerator = {c: sum(r[c] for r in rows) for c in applicable}
        denominator = sum(numerator.values())
        macro = {c: (sum(r[c] / sum(r[k] for k in applicable) for r in eligible)
                     / len(eligible) if eligible else None) for c in applicable}
        micro = {c: numerator[c] / denominator if denominator else None for c in applicable}
        result['dimensions'][field] = {
            'macro': {'rates': macro, 'eligible_cases': len(eligible), 'total_cases': total_cases,
                      'case_coverage': len(eligible) / total_cases if total_cases else None},
            'micro': {'rates': micro, 'numerators': numerator, 'denominator': denominator},
            'pending': sum(r['pending'] for r in rows),
            'not_applicable': sum(r.get('not_applicable', 0) for r in rows),
            'failed_cases': total_cases - len(measured),
            'rule': 'Macro uses fully reviewed applicable cases; micro uses reviewed applicable points. Pending/failures are not zero. No final comparison while provisional.'}
    return result


def completeness_before_after(records):
    """Keep B's denominator/coverage visible alongside each same-snapshot final answer."""
    before = next((r for r in records if r['arm'] == 'B'), None)
    if before is None:
        return []
    pairs = []
    for row in records:
        if row['arm'] not in {'C', 'D', 'C_REPAIR'}:
            continue
        if (not before.get('shared_snapshot_hash') or before.get('shared_snapshot_hash') != row.get('shared_snapshot_hash')
                or before['score']['case_hash'] != row['score']['case_hash']
                or before['score']['scorer_version'] != row['score']['scorer_version']):
            raise ValueError('unpaired_completeness_snapshot_or_case')
        pairs.append({'before_arm': 'B', 'after_arm': row['arm'],
                      'shared_snapshot_hash': row['shared_snapshot_hash'],
                      'before_output_hash': before['score']['output_hash'],
                      'after_output_hash': row['score']['output_hash'],
                      'before': before['score'].get('coverage'), 'after': row['score'].get('coverage'),
                      'before_status': before['score']['status'], 'after_status': row['score']['status'],
                      'not_a_quality_improvement_claim': True})
    return pairs
