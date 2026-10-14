"""P2 bounded workflow, reusing approved P1 research without changing P1 behavior."""
import hashlib
import json
from datetime import UTC, datetime
from time import monotonic

from langgraph.graph import END, START, StateGraph

from app.adapter import ROOT
from app.p1_contracts import AnalystDraft
from app.p1_workflow import P1Workflow
from app.p2_contracts import Lineage, MetricExtraction, P2State, RepairBatch, SemanticBatch
from app.reporting import final_report
from app.schemas import Gap
from app.tools.metrics import FORMULA_VERSION, FORMULAS, calculate_metric
from app.verification import adjudicate, detect_cash_conflicts, hard_checks, input_located


class P2Workflow(P1Workflow):
    async def planner(self, state):
        await super().planner(state)
        if state.plan and 'liquidity' in state.request.risk_categories and not state.plan.required_metrics:
            state.plan.required_metrics = list(FORMULAS)
        return state.model_dump()

    async def metrics_node(self, state):
        if not state.evidence or state.errors:
            return {}
        required = state.plan.required_metrics if state.plan else []
        try:
            extraction = MetricExtraction.model_validate(await self.model_call(
                state,self.adapter.extract,state.request,state.evidence,required))
            names=set()
            known={e.evidence_id:e for e in state.evidence}
            for item in extraction.requests:
                if item.name not in FORMULAS or item.name in names:
                    raise ValueError('metric_request_invalid')
                names.add(item.name)
                metric=calculate_metric(item.name,item.inputs,applicable=item.applicable)
                # Never admit invented extraction values as successful calculations.
                if metric.status=='success' and any(i.evidence_id not in known or
                        not input_located(i,known[i.evidence_id]) for i in item.inputs):
                    metric=metric.model_copy(update={'value':None,'status':'invalid_input',
                                                    'limitations':['input_source_not_located']})
                state.metrics.append(metric)
            for name in required:
                if name not in names:
                    state.metrics.append(calculate_metric(name,[]))
        except TimeoutError:
            self.failure(state,'run_timeout')
        except Exception:  # noqa: BLE001 - redact model data; failures become explicit gaps
            self.failure(state,'metric_extraction_error')
        return state.model_dump()

    async def analyst(self, state):
        if not state.evidence or monotonic()>=self.deadline:
            return {}
        try:
            draft=AnalystDraft.model_validate(await self.model_call(
                state,self.adapter.analyze,state.request,state.evidence,state.metrics))
            ids=set()
            for c in draft.claims:
                if c.claim_id in ids or c.risk_category not in state.request.risk_categories:
                    raise ValueError('claim_scope_or_id_invalid')
                ids.add(c.claim_id)
            # Invalid citation IDs are retained for Verifier and lineage, not accepted as facts.
            state.draft_claims=draft.claims
        except TimeoutError:
            self.failure(state,'run_timeout')
        except Exception:  # noqa: BLE001 - safe failure boundary
            self.failure(state,'analyst_contract_error')
        return state.model_dump()

    async def verify_claims(self, state, claims):
        hard={c.claim_id:hard_checks(c,state.evidence,state.metrics,self.tools.repository,
                                   state.request.as_of_date) for c in claims}
        eligible=[c for c in claims if all(hard[c.claim_id][0].values())]
        semantic={}
        if eligible:
            try:
                batch=SemanticBatch.model_validate(await self.model_call(
                    state,self.adapter.verify,eligible,state.evidence,state.metrics))
                semantic={d.claim_id:d for d in batch.decisions}
                if len(semantic)!=len(batch.decisions) or set(semantic)!={c.claim_id for c in eligible}:
                    raise ValueError('semantic_batch_ids_invalid')
            except TimeoutError:
                semantic={}
                state.gaps.append(Gap(code='run_timeout',message='Semantic call exceeded deadline.'))
            except Exception:  # noqa: BLE001 - no invented fallback decision
                semantic={}
                state.gaps.append(Gap(code='semantic_unavailable',message='Semantic verification failed or budget exhausted.'))
        config=json.loads((ROOT/'app/configuration/verification_p2_v1.json').read_text())
        by_e={e.evidence_id:e for e in state.evidence}
        by_m={m.metric_id:m for m in state.metrics}
        results=[]
        for c in claims:
            sources=set(c.evidence_ids)
            sources.update(i.evidence_id for mid in c.metric_ids if mid in by_m for i in by_m[mid].inputs)
            quality=min([config['source_quality'].get(
                getattr(self.tools.repository.document(by_e[eid].document_id),'source_type','unknown'),0)
                for eid in sources if eid in by_e] or [0])
            checks,reasons=hard[c.claim_id]
            decision=adjudicate(c,checks,reasons,semantic.get(c.claim_id),quality=quality,weights=config['weights'],
                detected_conflicts=detect_cash_conflicts(c,state.evidence,self.tools.repository,state.request.as_of_date))
            results.append(decision)
            state.lineage.append(Lineage(round=state.repair_count,claim=c,verification=decision,
                parent_claim_id=c.claim_id if state.repair_count else None))
        return results

    async def verifier(self, state):
        state.verification_results=await self.verify_claims(state,state.draft_claims)
        return state.model_dump()

    def after_verifier(self, state):
        partial=any(v.status=='partially_supported' for v in state.verification_results)
        available=(state.repair_count<self.settings.max_repair_rounds and
                   state.budget.llm_calls+2<=self.settings.max_llm_calls and monotonic()<self.deadline)
        return 'repair' if partial and available else 'report'

    async def route(self, state):
        # Async routing avoids executor completion callbacks in restricted WSL.
        return self.after_verifier(state)

    async def repair(self, state):
        targets={v.claim_id:v for v in state.verification_results if v.status=='partially_supported'}
        original={c.claim_id:c for c in state.draft_claims}
        state.repair_count+=1
        try:
            batch=RepairBatch.model_validate(await self.model_call(state,self.adapter.repair,
                [original[c] for c in targets],list(targets.values()),state.evidence,state.metrics))
            seen=set()
            for changed in batch.claims:
                if changed.claim_id not in targets or changed.claim_id in seen:
                    raise ValueError('repair_scope_invalid')
                seen.add(changed.claim_id)
                old=original[changed.claim_id]
                if (changed.claim_type,changed.risk_category,changed.evidence_ids,changed.metric_ids)!=(
                    old.claim_type,old.risk_category,old.evidence_ids,old.metric_ids):
                    raise ValueError('repair_citations_or_scope_changed')
            changed=[c for c in batch.claims if c != original[c.claim_id]]
            # All changes are reverified before any can enter the final report.
            results=await self.verify_claims(state,changed)
            revisions={c.claim_id:c for c in changed}
            decisions={v.claim_id:v for v in results}
            state.draft_claims=[revisions.get(c.claim_id,c) for c in state.draft_claims]
            state.verification_results=[decisions.get(v.claim_id,v) for v in state.verification_results]
            if not changed:
                state.gaps.append(Gap(code='repair_no_change',message='Repair produced no supported narrowing.'))
        except TimeoutError:
            state.gaps.append(Gap(code='run_timeout',message='Repair deadline exceeded.'))
        except Exception:  # noqa: BLE001 - untrusted repair never changes source authority
            state.gaps.append(Gap(code='repair_rejected',message='Repair failed schema or scope checks.'))
        return state.model_dump()

    async def report(self, state):
        if any(v.status=='partially_supported' for v in state.verification_results):
            state.gaps.append(Gap(code='repair_exhausted',message='Partial claims remain excluded after bounded repair.'))
        metadata=dict(self.metadata,stage='P2',
            label=('SIMULATED MODEL RESPONSES / '+getattr(self.adapter,'document_label','FIXED CORPUS DOCUMENTS'))
                if state.request.mode=='demo' else self.metadata.get('label'),schema_version='1+p1-v1+p2-v1',
            prompt_version='p2-v1',verification_version='p2-conflicts-v2',formula_version=FORMULA_VERSION,rubric_version='verification-p2-v1',
            model_calls=state.budget.llm_calls,tool_calls=state.budget.tool_calls,
            repair_count=state.repair_count,finished_at=datetime.now(UTC).isoformat(),
            corpus_hash=self.tools.repository.snapshot_hash(),
            actual_usage=None if state.request.mode=='demo' else getattr(self.adapter,'usage',None),
            cost=None,usage_reason='Simulated or provider usage unavailable; null is not zero.')
        paths=list((ROOT/'app/prompts').glob('*_p2_v1.txt'))+[
            ROOT/'app/configuration/verification_p2_v1.json', ROOT/'app/tools/metrics.py',
            ROOT/'tests/fixtures/p2_semantic_v1.json', ROOT/'tests/fixtures/regression/p2_adversarial_v2.json', ROOT/'tests/fixtures/regression/p2_adversarial_v1.json']
        metadata['p2_content_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        state.report=final_report(state,metadata)
        state.metadata=state.report.metadata
        return state.model_dump()

    def build(self):
        graph=StateGraph(P2State)
        for name,method in [('planner',self.planner),('research',self.research),
            ('metrics',self.metrics_node),('analyst',self.analyst),('verifier',self.verifier),
            ('repair',self.repair),('report',self.report)]:
            graph.add_node(name,method)
        graph.add_edge(START,'planner')
        for first,second in [('planner','research'),('research','metrics'),('metrics','analyst'),
                             ('analyst','verifier')]:
            graph.add_edge(first,second)
        # Repair reverifies every changed claim, retaining unchanged decisions.
        graph.add_conditional_edges('verifier',self.route,{'repair':'repair','report':'report'})
        graph.add_conditional_edges('repair',self.route,{'repair':'repair','report':'report'})
        graph.add_edge('report',END)
        return graph.compile()
