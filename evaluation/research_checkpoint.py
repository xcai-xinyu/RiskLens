"""Lossless Research context and source-validated engineering checkpoint recovery."""
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from app.p2_contracts import P2State
from app.planning import validate_plan
from app.schemas import Evidence
from evaluation.candidate_data import file_hash, fingerprint, request_only
from evaluation.research_budget import BudgetModel, ExplicitBudgetExecution


class LosslessResearchModel(BudgetModel):
    def __init__(self, adapter, execution):
        super().__init__(adapter, execution)
        self.query_trail = copy.deepcopy(getattr(execution, 'restored_queries', []))
        self.research_evidence = []

    def prepare(self, role, data):
        if role == 'research':
            data = copy.deepcopy(data)
            data['retained_evidence'] = self.research_evidence
            data['executed_queries'] = self.query_trail
            data['research_context_version'] = 'lossless-research-v3'
            data['context_note'] = ('Complete unique returned quotes are supplied in retained_evidence. '
                'All original tool responses remain immutable in State/Repository and audit. '
                'Old dialogue wrappers are not additional evidence. Preserve adverse evidence. '
                'Use the explicit remaining budget and existing query trail; do not repeat searches.')
        return super().prepare(role, data)

    async def research_turn(self, context):
        self.research_evidence = self.evidence_rows([
            Evidence.model_validate(raw) for e in context['tool_events']
            for raw in (e['result'].get('data') or [])])
        # Preserve the current native function_call + reasoning items (in memory
        # only) required by Responses, but do not resend every prior exchange.
        # All financial evidence and executed query arguments are preserved above.
        if getattr(self.adapter, 'pending_tool', None):
            history = self.adapter.history
            last_user = max(i for i,item in enumerate(history) if item.get('role')=='user')
            current = copy.deepcopy(history[last_user+1:])
            if not any(i.get('type')=='function_call' and i.get('call_id')==self.adapter.pending_tool for i in current):
                raise ValueError('pending_tool_history_invalid')
            self.adapter.history = [copy.deepcopy(history[0]), *current]
        elif hasattr(self.adapter, 'history'):
            self.adapter.history = []
        turn = await super().research_turn(context)
        if turn.action=='tool':
            self.query_trail.append(turn.tool_call.model_dump(mode='json'))
        self.coverage['research_lossless'] = {
            'evidence_ids':[e['evidence_id'] for e in self.research_evidence],
            'quote_hashes':{e['evidence_id']:e['quote_sha256'] for e in self.research_evidence},
            'executed_queries':copy.deepcopy(self.query_trail),
            'dropped_evidence':[], 'dialogue_policy':'current-native-exchange-plus-full-unique-evidence'}
        return turn


class LosslessResearchExecution(ExplicitBudgetExecution):
    def configuration(self):
        r=super().configuration()
        r.update(research_context_version='lossless-research-v3',
                 history_policy='current-native-exchange-plus-full-unique-evidence')
        r['prompt_version']+='-lossless-research-v3'
        return r

    def model_wrapper(self, adapter):
        self.model=LosslessResearchModel(adapter,self)
        return self.model


@dataclass(frozen=True)
class ResearchResume:
    state: dict
    request_hash: str
    source_hash: str
    queries: list
    provenance: dict

    def materialize(self, job, repository):
        from evaluation.frozen_runner import source_identity
        if (fingerprint(request_only(job.request).model_dump(mode='json'))!=self.request_hash or
                fingerprint(source_identity(job.sources))!=self.source_hash):
            raise ValueError('research_resume_scope_drift')
        state=P2State.model_validate(copy.deepcopy(self.state))
        validate_plan(state.plan)
        if (state.run_id!=job.case_id or state.plan.company_id!=job.sources[0].company_group or
                state.budget.tool_calls!=len(state.tool_events) or
                len(self.queries)!=len(state.tool_events) or
                [e.code for e in state.errors]!=['model_or_tool_error']):
            raise ValueError('research_resume_state_invalid')
        # Old generic wrapper error is retained in sealed origin/provenance, not
        # carried as a new error. No semantic gap or evidence is silently removed.
        state.errors=[]
        state.gaps=[g for g in state.gaps if g.code!='model_or_tool_error']
        for e in state.evidence:
            repository.trace(e,job.request.as_of_date)
        for event in state.tool_events:
            for raw in event.result.data or []:
                repository.trace(Evidence.model_validate(raw),job.request.as_of_date)
        return state


def load_research_resume(directory, job, *, prior=None):
    """Controller reads only source-bound output/state, never reference annotations."""
    from evaluation.frozen_batch import validate_seal
    from evaluation.frozen_runner import source_identity
    directory=Path(directory)
    validate_seal(directory/job.case_id)
    result=json.loads((directory/job.case_id/'runtime-output.json').read_text())['result']
    if result.get('error')!='CONTEXT_BUDGET_BLOCKED' or result.get('shared_snapshot'):
        raise ValueError('research_resume_not_eligible')
    state_path=directory/(job.case_id+'-stages')/'research.json'
    state=json.loads(state_path.read_text())
    if request_only(state['request'])!=job.request:
        raise ValueError('research_resume_request_drift')
    queries=[]
    for packet in result['context_execution']['requests']:
        if packet['role']=='research' and packet['status']=='COMPLETED':
            for item in (packet.get('provider_output') or {}).get('output',[]):
                if item.get('type')=='function_call':
                    queries.append({'name':item['name'],'arguments':json.loads(item['arguments'])})
    if prior is not None:
        # Explicit chain, not a filename inference. Every inherited tool result
        # must be byte-equivalent in the next saved State before importing queries.
        n = len(prior.queries)
        if (prior.request_hash != fingerprint(request_only(job.request).model_dump(mode='json')) or
                state['tool_events'][:n] != prior.state['tool_events'] or
                len(state['tool_events']) != n + len(queries)):
            raise ValueError('research_resume_ancestor_mismatch')
        queries = copy.deepcopy(prior.queries) + queries
    if len(queries)!=len(state['tool_events']):
        raise ValueError('research_resume_query_history_incomplete')
    return ResearchResume(state,fingerprint(request_only(job.request).model_dump(mode='json')),
        fingerprint(source_identity(job.sources)),queries,
        {'source':str(directory/job.case_id),'ancestor':prior.provenance if prior else None,'output_seal_sha256':file_hash(directory/job.case_id/'OUTPUT_SEAL.json'),
         'state_sha256':file_hash(state_path),'evidence_count':len(state['evidence']),
         'completed_searches':len(queries),'old_failure':'CONTEXT_BUDGET_BLOCKED','cleared_transient_wrapper_error':'model_or_tool_error',
         'continued_from':'Research before next unsent turn; Planner and existing results reused',
         'configuration_change':'lossless-research-v3; source contents and retrieval unchanged'})
