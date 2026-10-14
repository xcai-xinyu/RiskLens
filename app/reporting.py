"""Deterministic reports: only supported final claims; no generated summary."""
import json

from app.schemas import Gap, RiskFactor, RiskReport


def final_report(state, metadata):
    decisions = {v.claim_id:v for v in state.verification_results}
    claims = sorted([c for c in state.draft_claims if c.claim_id in decisions and
                     decisions[c.claim_id].status == 'supported'], key=lambda c:c.claim_id)
    gaps = list(state.gaps)
    for claim in state.draft_claims:
        decision = decisions.get(claim.claim_id)
        if decision is None or decision.status != 'supported':
            gaps.append(Gap(code='claim_'+(decision.status if decision else 'unverifiable'),
                message=f'{claim.claim_id}: {decision.explanation if decision else "No final decision."}'))
    for metric in state.metrics:
        if metric.status != 'success':
            gaps.append(Gap(code=metric.status,message=f'{metric.name}: '+', '.join(metric.limitations)))
    # This corpus has no reviewed complete category risk rubric. Abstain, not LOW.
    gaps.append(Gap(code='risk_level_unknown',message='Insufficient reviewed category risk signals.'))
    used = {eid for c in claims for eid in c.evidence_ids}
    used.update(i.evidence_id for m in state.metrics for i in m.inputs
                if any(m.metric_id in c.metric_ids for c in claims))
    ev = {e.evidence_id:e for e in state.evidence}
    sources = sorted([d for d in state.documents if d.published_at and
        d.published_at <= state.request.as_of_date and any(
            ev[eid].document_id == d.document_id for eid in used if eid in ev)],key=lambda d:d.document_id)
    citations = {eid:{'number':n, **ev[eid].model_dump(mode='json')} for n,eid in enumerate(sorted(used),1) if eid in ev}
    metadata = dict(metadata, citations=citations,
        verification_results=[v.model_dump(mode='json') for v in state.verification_results],
        claim_lineage=[l.model_dump(mode='json') for l in state.lineage],
        claim_coverage={'original':len({l.claim.claim_id for l in state.lineage if l.round==0}),
                        'supported_final':len(claims)},verification_implemented=True)
    return RiskReport(run_id=state.run_id,company=state.request.company,
        as_of_date=state.request.as_of_date,status='partial' if claims else 'insufficient_evidence',
        overall_risk='UNKNOWN',summary='\n'.join(c.text for c in claims),claims=claims,
        metrics=state.metrics,sources=sources,gaps=gaps,metadata=metadata,
        risk_factors=[RiskFactor(category=c,level='UNKNOWN',
            verified_claim_ids=[x.claim_id for x in claims if x.risk_category==c],
            rationale='Verified observations retained; insufficient reviewed signals to assign a risk level.',
            missing_data=['Category risk evidence/rubric coverage']) for c in state.request.risk_categories])


def render_json(report):
    return json.dumps(report.model_dump(mode='json'),ensure_ascii=False,sort_keys=True,indent=2)+'\n'


def render_markdown(report):
    def safe(text):
        return str(text).replace('<','&lt;').replace('>','&gt;').replace('|','\\|').replace('\n',' ')
    lines = [f'# RiskLens — {safe(report.company)}',
             f'As of: {report.as_of_date} | Status: {report.status} | Risk: {report.overall_risk}',
             f'Mode: {safe(report.metadata.get("label", report.metadata.get("mode")))}',
             '', '## Verified claims']
    citations=report.metadata.get('citations',{})
    for claim in report.claims:
        metric_sources={i.evidence_id for m in report.metrics if m.metric_id in claim.metric_ids for i in m.inputs}
        ids=sorted(set(claim.evidence_ids)|metric_sources)
        refs=' '.join(f'[{citations[e]["number"]}]' for e in ids if e in citations)
        lines.append(f'- {safe(claim.claim_id)} ({claim.claim_type}, supported): {safe(claim.text)} {refs}')
    lines += ['', '## Calculations']
    for m in report.metrics:
        lines.append(f'- {safe(m.metric_id)} / {safe(m.name)}: {m.value} ({m.status}); {safe(m.formula)}')
        for i in m.inputs:
            lines.append(f'  - {safe(i.name)}={i.value} {safe(i.unit)} {safe(i.currency)}; {safe(i.period)}; {safe(i.evidence_id)}')
        lines.append('  - Limitations: '+safe(', '.join(m.limitations)))
    lines += ['', '## Sources and original quotes']
    docs={d.document_id:d for d in report.sources}
    for eid, c in citations.items():
        doc=docs[c['document_id']]
        lines.append(f'[{c["number"]}] {safe(eid)} — {safe(doc.title)}; published {doc.published_at}; '
            f'page {c["page"]} / section {safe(c["section"])}; {safe(doc.url or doc.local_path)}')
        lines.append('> '+safe(c['quote']))
    lines += ['', '## Verification and gaps']
    for v in report.metadata.get('verification_results',[]):
        lines.append(f'- {safe(v["claim_id"])}: {v["status"]}; {safe(v["explanation"])}; conflicts: {safe(v["conflicts"])}')
    lines.extend(f'- {safe(g.code)}: {safe(g.message)}' for g in report.gaps)
    lines += ['', '## Lineage']
    for l in report.metadata.get('claim_lineage',[]):
        lines.append(f'- Round {l["round"]}: {safe(l["claim"]["claim_id"])}; {safe(l["claim"]["text"])}; {l["verification"]["status"]}')
    return '\n'.join(lines)+'\n'
