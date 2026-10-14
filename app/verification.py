"""Hard gates before semantic adjudication; no hard failure can be scored away."""
import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, localcontext

from app.schemas import VerificationResult
from app.tools.metrics import calculate_metric, parse_number


def input_located(item, evidence):
    """Necessary source-token checks; association/definition is also semantically verified."""
    if item.value is None:
        return False
    values = []
    for token in re.findall(r'\(?-?\d[\d,]*(?:\.\d+)?%?\)?', evidence.quote):
        try:
            values.append(parse_number(token))
        except Exception:  # noqa: BLE001, S112 - malformed untrusted numeric tokens are not inputs
            continue
    currency = item.currency and item.currency in evidence.quote
    unit = item.unit == 'units' or item.unit.lower() in evidence.quote.lower()
    period = item.period in evidence.quote
    return item.value in values and bool(currency) and unit and period


def hard_checks(claim, evidence, metrics, repository, cutoff):
    by_evidence = {e.evidence_id:e for e in evidence}
    by_metric = {m.metric_id:m for m in metrics}
    checks = {'citation_ids':bool(claim.evidence_ids or claim.metric_ids),
              'quote_location':True, 'cutoff_date':True, 'metric_inputs':True,
              'unit_period_currency':True, 'recalculable':True, 'numerical_consistency':True}
    reasons = []
    referenced = set(claim.evidence_ids)
    for mid in claim.metric_ids:
        metric = by_metric.get(mid)
        if metric is None:
            checks['citation_ids'] = False
            continue
        referenced.update(i.evidence_id for i in metric.inputs)
        try:
            recalc = calculate_metric(metric.name, metric.inputs)
            checks['recalculable'] &= (metric.status == 'success' and recalc == metric)
            checks['unit_period_currency'] &= recalc.status == 'success'
        except ValueError:
            checks['recalculable'] = False
        for item in metric.inputs:
            source = by_evidence.get(item.evidence_id)
            checks['metric_inputs'] &= source is not None and input_located(item, source)
    for eid in referenced:
        source = by_evidence.get(eid)
        if source is None:
            checks['citation_ids'] = False
            continue
        try:
            repository.trace(source, cutoff)
        except ValueError as exc:
            code = str(exc)
            reasons.append(code)
            checks['cutoff_date' if code == 'citation_date_invalid' else 'quote_location'] = False
    if claim.metric_ids:
        allowed=set()
        for mid in claim.metric_ids:
            metric=by_metric.get(mid)
            if metric is not None:
                if metric.value is not None:
                    allowed.add(metric.value)
                for item in metric.inputs:
                    if item.value is not None:
                        allowed.add(item.value)
                    allowed.update(Decimal(n) for n in re.findall(r'\d+',item.period))
        for eid in claim.evidence_ids:
            if eid in by_evidence:
                for token in re.findall(r'\(?-?\d[\d,]*(?:\.\d+)?%?\)?',by_evidence[eid].quote):
                    try:
                        allowed.add(parse_number(token))
                    except Exception:  # noqa: BLE001, S110 - ignore malformed source tokens
                        pass
        for token in re.findall(r'\(?-?\d[\d,]*(?:\.\d+)?%?\)?',claim.text):
            try:
                checks['numerical_consistency'] &= parse_number(token) in allowed
            except Exception:  # noqa: BLE001 - malformed claim number fails closed
                checks['numerical_consistency']=False
    reasons.extend(key for key,passed in checks.items() if not passed)
    return checks, reasons



def detect_cash_conflicts(claim, evidence, repository, cutoff):
    """Conservative explicit cash observations; other definitions stay semantic.

    Compare only cash, identical explicit period/currency, traceable dated sources.
    Never equate cash with USD resources or cash-and-cash-equivalents definitions.
    """
    pattern = re.compile(
        r'\bCash\s+(?:is|of)\s+(?P<value>\(?-?\d[\d,]*(?:\.\d+)?\)?)'
        r'\s+(?P<unit>units|thousand|million|billion)\s+(?P<currency>[A-Z]{3})'
        r'\s+(?P<period>FY:?[0-9]{4}|Q[1-4]:?[0-9]{4})', re.IGNORECASE)
    targets={(m['currency'].upper(),m['period'].upper().replace(':',''))
             for m in pattern.finditer(claim.text)}
    if not targets:
        return []
    observations=[]
    from app.tools.metrics import UNITS
    for source in evidence:
        try:
            repository.trace(source,cutoff)
        except ValueError:
            continue  # Invalid or later evidence cannot establish a conflict.
        for match in pattern.finditer(source.quote):
            key=(match['currency'].upper(),match['period'].upper().replace(':',''))
            if key in targets:
                try:
                    with localcontext(Context(prec=34,rounding=ROUND_HALF_EVEN)):
                        value=parse_number(match['value'])*UNITS[match['unit'].lower()]
                except DecimalException:
                    continue  # Malformed untrusted numeric notation is not an observation.
                observations.append((key,value,source,match.group()))
    conflicts=[]
    for index,(key,value,source,quote) in enumerate(observations):
        for other_key,other_value,other,other_quote in observations[index+1:]:
            if key!=other_key or value==other_value or source.document_id==other.document_id:
                continue
            message=(f'cash conflict {key[0]} {key[1]}: '
                f'{source.evidence_id} ({source.document_id}, page={source.page}, section={source.section}) '
                f'"{quote}" [{value} units] vs '
                f'{other.evidence_id} ({other.document_id}, page={other.page}, section={other.section}) '
                f'"{other_quote}" [{other_value} units]')
            if message not in conflicts:
                conflicts.append(message)
    return conflicts

def adjudicate(claim, checks, reasons, semantic=None, *, quality=0, weights=None, detected_conflicts=None):
    status, score = 'unverifiable', None
    explanation = '; '.join(reasons) or 'Semantic verification unavailable.'
    conflicts = list(detected_conflicts or [])
    checks = dict(checks, no_conflicts=not conflicts)
    if not all(value for key,value in checks.items() if key != "no_conflicts") :
        status = 'unsupported' if not checks['citation_ids'] or not checks['quote_location'] else 'unverifiable'
    elif semantic is not None:
        status, explanation = semantic.status, semantic.explanation
        conflicts = list(dict.fromkeys(conflicts + semantic.conflicts))
        overstatement = semantic.overstatement or bool(re.search(
            r'\bliquidity\s+is\s+guaranteed\s+safe\b',claim.text,re.IGNORECASE))
        checks = dict(checks, semantic_support=status == 'supported',
                      no_overstatement=not overstatement, no_conflicts=not conflicts)
        if overstatement:
            explanation += ' Overstatement must be narrowed and reverified.'
        if overstatement and status == 'supported':
            status = 'partially_supported'
        if conflicts and status in ('supported','partially_supported'):
            status = 'unverifiable'
            explanation += ' Unresolved conflicts require abstention.'
        checks['semantic_support'] = status == 'supported'
        if status in ('supported','partially_supported'):
            # No claim of independent corroboration from group count alone.
            weights = weights or {'entailment':0.5,'source_quality':0.3,'independent_corroboration':0.2}
            with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
                score = float(Decimal(100)*(Decimal(str(weights['entailment']))*(1 if status=='supported' else Decimal('0.5'))
                                            + Decimal(str(weights['source_quality']))*Decimal(str(quality))))
    if conflicts and status in ('supported','partially_supported','unverifiable'):
        status, score = 'unverifiable', None
        checks['semantic_support'] = False
    if conflicts:
        explanation += ' Conflicts: ' + '; '.join(conflicts)
    return VerificationResult(claim_id=claim.claim_id,status=status,checks=checks,
        evidence_support_score=score,explanation=explanation,conflicts=conflicts)
