"""Independent exact-reference scoring. Never uses an arm's Verifier status as gold.
Unknown semantic paraphrases remain unresolved; exact matching is engineering-only.
"""
from decimal import Decimal

VERSION='p4-exact-reference-v1'


def ratio(numerator,denominator):
    return {'numerator':numerator,'denominator':denominator,
            'value':numerator/denominator if denominator else None,
            'status':'MEASURED' if denominator else 'N/A'}


def score(case,output,evidence_ids):
    if output['status']=='failed':
        return {'status':'failed','metrics':{},'unresolved_claims':[], 'reason':'Failed arm retained; quality metrics N/A, coverage reports failure.'}
    claims=output['claims'];by_metric={m.get('metric_id'):m for m in output['metrics']}
    def references(claim):
        ids=set(claim['evidence_ids'])
        ids.update(i['evidence_id'] for mid in claim.get('metric_ids',[]) if mid in by_metric for i in by_metric[mid]['inputs'])
        return sorted(ids)
    judgments={j['text']:j for j in case['judgments']}
    facts=[c for c in claims if c['claim_type']=='fact']
    coverage=sum(bool(references(c)) and all(e in evidence_ids for e in references(c)) for c in facts)
    checked=correct=unsupported=reviewed=0;unresolved=[]
    for claim in claims:
        gold=judgments.get(claim['text'])
        if gold is None:
            unresolved.append(claim);continue
        reviewed+=1;unsupported+=gold['verdict'] in ('unsupported','contradicted')
        for eid in references(claim):
            checked+=1
            correct+=gold['verdict']=='supported' and eid in gold['evidence_ids'] and eid in evidence_ids
    reference={r['text']:r for r in case['reference_claims']}
    covered={c['text'] for c in claims if c['text'] in reference and
             c['claim_type']==reference[c['text']]['claim_type'] and references(c) and
             all(e in evidence_ids and e in reference[c['text']]['evidence_ids'] for e in references(c))}
    expected=case['reference_metrics'];actual={m['name']:m for m in output['metrics']}
    numeric_ok=numeric_total=validation_ok=0
    for ref in expected:
        metric=actual.get(ref['name']);success=ref['value'] is not None
        same=metric is not None and metric['status']==ref['status'] and metric['formula']==ref['formula']
        if metric is not None:
            same &= metric['inputs']==ref['inputs']  # values, units, currency, period, IDs
            same &= (metric['value'] is None and ref['value'] is None) or (
                metric['value'] is not None and ref['value'] is not None and Decimal(str(metric['value']))==Decimal(str(ref['value'])))
        validation_ok+=bool(same)
        if success:numeric_total+=1;numeric_ok+=bool(same)
    abstain=not claims
    expected_abstention=case['expected_abstention']
    gaps=set(output.get('gaps',[]))
    abstention_correct=abstain and set(case['expected_gaps'])<=gaps
    return {'status':output['status'],'metrics':{
        'citation_coverage':ratio(coverage,len(facts)),
        'citation_correctness':ratio(correct,checked),
        'unsupported_rate':ratio(unsupported,reviewed),
        'numerical_accuracy':ratio(numeric_ok,numeric_total),
        'metric_validation_accuracy':ratio(validation_ok,len(expected)),
        'completeness':ratio(len(covered),len(reference)),
        'abstention_accuracy':ratio(int(abstention_correct),1 if expected_abstention else 0),
        'answerable_false_abstention':ratio(int(abstain),0 if expected_abstention else 1)},
        'unresolved_claims':unresolved,'scorable_claims':reviewed,'total_claims':len(claims),
        'annotation_status':case['annotation_status']}


def aggregate(records):
    result={}
    for arm in ('A','B','C','D','C_REPAIR'):
        rows=[r for r in records if r['arm']==arm]
        names=sorted({k for r in rows for k in r['score']['metrics']})
        metrics={name:ratio(sum(r['score']['metrics'].get(name,{}).get('numerator',0) for r in rows),
                            sum(r['score']['metrics'].get(name,{}).get('denominator',0) for r in rows)) for name in names}
        usages=[u for r in rows if r.get('actual_usage') is not None for u in r['actual_usage']]
        unknown=any(r.get('actual_usage') is None or any(u is None for u in r.get('actual_usage') or []) for r in rows)
        tokens=None if unknown else {key:sum(u.get(key,0) for u in usages) for key in ('input_tokens','output_tokens','total_tokens')}
        result[arm]={'cases':len(rows),'failed':sum(r['status']=='failed' for r in rows),
            'completed':sum(r['status']=='completed' for r in rows),
            'incomplete':sum(r['status']=='partial' for r in rows),
            'unresolved_claims':sum(len(r['score']['unresolved_claims']) for r in rows),
            'metrics':metrics,'latency_seconds':sum(r['latency_seconds'] for r in rows),
            'llm_calls':sum(r['llm_calls'] for r in rows),'token_usage':tokens,'cost':None,
            'usage_reason':'Demo usage/cost unknown; not zero. Live token usage retained per case, no unsupported price estimate.',
            'aggregation':'Micro numerators/denominators; failed arms retained in coverage and excluded from unavailable quality denominators.'}
    return result
