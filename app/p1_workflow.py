"""P1 Planner -> bounded Research -> Analyst draft; no P2 decisions."""
import asyncio
from datetime import UTC, datetime
from time import monotonic

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.p1_contracts import AnalystDraft, P1State, ResearchTurn, ToolEvent
from app.planning import validate_plan
from app.schemas import Evidence, Gap, RiskFactor, RiskReport


class P1Workflow:
    def __init__(self, adapter, tools, settings, company_id, deadline, metadata):
        self.adapter, self.tools, self.settings = adapter, tools, settings
        self.company_id, self.deadline, self.metadata = company_id, deadline, metadata

    async def model_call(self, state, method, *args):
        if state.budget.llm_calls >= self.settings.max_llm_calls:
            raise ValueError('llm_budget_exhausted')
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        state.budget.llm_calls += 1
        return await asyncio.wait_for(method(*args), timeout=min(30, remaining))

    def failure(self, state, code):
        gap = Gap(code=code, message={'run_timeout':'Run deadline exceeded.',
            'llm_budget_exhausted':'Model budget exhausted.'}.get(code,'P1 stage failed; no fallback used.'))
        state.gaps.append(gap)
        state.errors.append(gap)

    async def planner(self, state):
        try:
            plan = validate_plan(await self.model_call(
                state,self.adapter.make_plan,state.request,self.company_id))
            if plan.company_id != self.company_id:
                raise ValueError('company_override_forbidden')
            state.plan = plan
        except TimeoutError:
            self.failure(state,'run_timeout')
        except ValidationError:
            self.failure(state,'model_parse_error')
        except ValueError as exc:
            code = str(exc) if str(exc) in ('llm_budget_exhausted','company_override_forbidden') else 'model_parse_error'
            self.failure(state,code)
        except Exception:  # noqa: BLE001 - ModelAdapter boundary redacts provider errors
            self.failure(state,'model_error')
        return state.model_dump()

    async def research(self, state):
        if state.errors or state.plan is None:
            return {}
        seen_calls = set()
        try:
            while True:
                # Every turn consumes a model call. Preserve one for the Analyst.
                if state.budget.llm_calls >= self.settings.max_llm_calls - 1:
                    self.failure(state,'llm_budget_exhausted')
                    break
                if state.budget.tool_calls >= self.settings.max_tool_calls:
                    self.failure(state,'tool_budget_exhausted')
                    break
                context = {'request':state.request.model_dump(mode='json'),
                    'plan':state.plan.model_dump(mode='json'), 'company_id':self.company_id,
                    'tool_events':[e.model_dump(mode='json') for e in state.tool_events],
                    'budget':state.budget.model_dump()}
                turn = ResearchTurn.model_validate(await self.model_call(
                    state,self.adapter.research_turn,context))
                if turn.action=='finish':
                    if turn.tool_call is not None:
                        raise ValueError('research_turn_invalid')
                    break
                if turn.tool_call is None:
                    raise ValueError('research_turn_invalid')
                key = turn.tool_call.model_dump_json()
                if key in seen_calls:
                    self.failure(state,'repeated_tool_call')
                    break
                seen_calls.add(key)
                remaining = self.deadline-monotonic()
                if remaining <= 0:
                    raise TimeoutError
                state.budget.tool_calls += 1
                result = await asyncio.wait_for(self.tools.execute(turn.tool_call),
                                                timeout=min(20,remaining))
                state.tool_events.append(ToolEvent(name=turn.tool_call.name,result=result))
                if result.status == 'error':
                    state.gaps.append(Gap(code=result.error_code,message='Research tool returned an explicit error.'))
                elif result.status == 'empty':
                    state.gaps.append(Gap(code='empty_result',message='Research tool returned no evidence.'))
                elif turn.tool_call.name=='search_documents':
                    known = {e.evidence_id for e in state.evidence}
                    for raw in result.data:
                        item = Evidence.model_validate(raw)
                        self.tools.repository.trace(item,state.request.as_of_date)
                        if item.evidence_id not in known and len(state.evidence)<self.settings.retrieval_limit:
                            state.evidence.append(item)
                            known.add(item.evidence_id)
                state.documents = self.tools.repository.documents()
                state.document_ids = [d.document_id for d in state.documents]
        except TimeoutError:
            self.failure(state,'run_timeout')
        except ValidationError:
            self.failure(state,'model_parse_error')
        except ValueError as exc:
            self.failure(state,'llm_budget_exhausted' if str(exc)=='llm_budget_exhausted' else 'research_contract_error')
        except Exception:  # noqa: BLE001 - never log untrusted provider/tool exception text
            self.failure(state,'model_or_tool_error')
        return state.model_dump()

    async def analyst(self, state):
        if not state.evidence or monotonic()>=self.deadline:
            return {}
        try:
            draft = AnalystDraft.model_validate(await self.model_call(
                state,self.adapter.analyze,state.request,state.evidence,state.metrics))
            known = {e.evidence_id:e for e in state.evidence}
            seen = set()
            for claim in draft.claims:
                if claim.claim_id in seen or claim.risk_category not in state.request.risk_categories:
                    raise ValueError('claim_scope_or_id_invalid')
                seen.add(claim.claim_id)
                if not claim.evidence_ids or claim.metric_ids or any(e not in known for e in claim.evidence_ids):
                    raise ValueError('claim_citation_invalid')
                # Source tracing happened at Research admission. Analyst consumes
                # validated in-memory Evidence only; it cannot access Repository.
            # Keep invalid batches out of draft_claims. No P2 semantic verification is claimed.
            state.draft_claims = draft.claims
        except TimeoutError:
            self.failure(state,'run_timeout')
        except ValidationError:
            self.failure(state,'model_parse_error')
        except ValueError as exc:
            code = str(exc) if str(exc) in ('claim_scope_or_id_invalid','claim_citation_invalid',
                                            'llm_budget_exhausted') else 'analyst_contract_error'
            self.failure(state,code)
        except Exception:  # noqa: BLE001 - preserve adapter isolation
            self.failure(state,'model_error')
        return state.model_dump()

    async def envelope(self, state):
        state.gaps.append(Gap(code='p2_verification_pending',
                              message='Analyst claims are drafts, not verified financial conclusions.'))
        metadata = dict(self.metadata, model_calls=state.budget.llm_calls,
            tool_calls=state.budget.tool_calls, finished_at=datetime.now(UTC).isoformat(),
            corpus_hash=self.tools.repository.snapshot_hash(),
            actual_usage=None if state.request.mode=='demo' else getattr(self.adapter,'usage',None),cost=None,
            embedding_calls=getattr(self.tools.retriever.embedding,'calls',None),
            usage_reason='Simulated or provider usage unavailable; null is not zero.',
            claim_status='DRAFT_UNVERIFIED',verification_implemented=False)
        state.metadata = metadata
        state.report = RiskReport(run_id=state.run_id,company=state.request.company,
            as_of_date=state.request.as_of_date,
            status='partial' if state.evidence else 'insufficient_evidence',overall_risk='UNKNOWN',
            summary='',claims=[],metrics=[],sources=[d for d in state.documents if d.published_at and
                d.published_at<=state.request.as_of_date and any(e.document_id==d.document_id for e in state.evidence)],
            gaps=state.gaps,metadata=metadata,risk_factors=[RiskFactor(category=c,level='UNKNOWN',
                verified_claim_ids=[],rationale='P1 provides unverified research drafts only.',
                missing_data=['P2 verification and metrics']) for c in state.request.risk_categories])
        return state.model_dump()

    def build(self):
        graph = StateGraph(P1State)
        for name in ('planner','research','analyst','envelope'):
            graph.add_node(name,getattr(self,name))
        graph.add_edge(START,'planner')
        graph.add_edge('planner','research')
        graph.add_edge('research','analyst')
        graph.add_edge('analyst','envelope')
        graph.add_edge('envelope',END)
        return graph.compile()
