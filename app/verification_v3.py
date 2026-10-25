"""Opt-in numerical display revision; all legacy provenance/arithmetic gates remain."""
import json

from app.adapter import ROOT
from app.numeric_display import inspect_display
from app.p2_contracts import Lineage, SemanticBatch
from app.schemas import Gap
from app.verification import adjudicate, detect_cash_conflicts
from app.verification import hard_checks as legacy_checks

VERSION = 'p2-metric-bound-display-v3'


def hard_checks(claim, evidence, metrics, repository, cutoff):
    checks, reasons = legacy_checks(claim, evidence, metrics, repository, cutoff)
    if claim.metric_ids:
        display = inspect_display(claim, metrics)
        checks['numerical_consistency'] = display['passed']
        reasons = [r for r in reasons if r != 'numerical_consistency']
        if not display['passed']:
            reasons.append('numerical_consistency')
    return checks, reasons


async def verify_claims(workflow, state, claims):
    """Same P2 adjudication/lineage, with an explicitly selected hard-check version."""
    repo = workflow.tools.repository
    checker = getattr(workflow, 'hard_checks', hard_checks)
    hard = {c.claim_id: checker(c, state.evidence, state.metrics, repo,
                                   state.request.as_of_date) for c in claims}
    eligible = [c for c in claims if all(hard[c.claim_id][0].values())]
    semantic = {}
    if eligible:
        try:
            batch = SemanticBatch.model_validate(await workflow.model_call(
                state, workflow.adapter.verify, eligible, state.evidence, state.metrics))
            semantic = {d.claim_id: d for d in batch.decisions}
            if len(semantic) != len(batch.decisions) or set(semantic) != {c.claim_id for c in eligible}:
                raise ValueError('semantic_batch_ids_invalid')
        except TimeoutError:
            semantic = {}
            state.gaps.append(Gap(code='run_timeout', message='Semantic call exceeded deadline.'))
        except Exception:  # noqa: BLE001 - preserve legacy no-fallback failure behavior
            semantic = {}
            state.gaps.append(Gap(code='semantic_unavailable', message='Semantic verification failed or budget exhausted.'))
    config = json.loads((ROOT/'app/configuration/verification_p2_v1.json').read_text())
    by_e = {e.evidence_id: e for e in state.evidence}
    by_m = {m.metric_id: m for m in state.metrics}
    results = []
    for c in claims:
        ids = set(c.evidence_ids)
        ids.update(i.evidence_id for mid in c.metric_ids if mid in by_m for i in by_m[mid].inputs)
        quality = min([config['source_quality'].get(
            getattr(repo.document(by_e[eid].document_id), 'source_type', 'unknown'), 0)
            for eid in ids if eid in by_e] or [0])
        checks, reasons = hard[c.claim_id]
        decision = adjudicate(c, checks, reasons, semantic.get(c.claim_id), quality=quality,
            weights=config['weights'], detected_conflicts=detect_cash_conflicts(
                c, state.evidence, repo, state.request.as_of_date))
        results.append(decision)
        state.lineage.append(Lineage(round=state.repair_count, claim=c, verification=decision,
            parent_claim_id=c.claim_id if state.repair_count else None))
    return results
