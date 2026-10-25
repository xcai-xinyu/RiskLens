"""Opt-in budget wire v2. Counts are observations; only executors grant admission."""
import copy
import re
from decimal import Decimal
from time import monotonic

from evaluation.context_runtime import ContextModel, ContextWorkflow
from evaluation.ten_batch_budget import TenExecution

VERSION = 'explicit-node-budget-v2'
INSTRUCTION = (
    'Budget contract v2: *_used counts consumption, never remaining capacity. '
    '*_limit is a cap; *_remaining is computed by the executor. '
    'tool_calls_remaining > 0 means tool calls are still available, subject to the '
    'independent Chat, cost and time limits. Do not infer exhaustion from used=0. '
    'The executor enforces all limits. A finish without retrieval is allowed, but '
    'explain its actual reason; absence of retrieval is not absence of disclosure.'
)


def snapshot(ledger, tool_used, *, clock=monotonic):
    limits = ledger.limits
    pending = Decimal(ledger.pending['reserved_usd']) if ledger.pending else Decimal(0)
    holds = ledger.unknown_holds
    remaining_time = max(0, ledger.deadline-clock()) if ledger.case_id else 0
    b = {'version': VERSION, 'observation': 'before_current_model_request',
         'authority': 'executor_only', 'tool_calls_used': tool_used,
         'tool_calls_limit': 4, 'tool_calls_remaining': max(0, 4-tool_used),
         'cost_usd_used': str(ledger.total), 'cost_usd_limit': str(limits.usd),
         'cost_usd_reserved': str(pending+holds),
         'cost_usd_remaining': str(max(Decimal(0), limits.usd-ledger.total-pending-holds)),
         'case_seconds_used': round(ledger.case_seconds_limit-remaining_time, 3),
         'case_seconds_limit': ledger.case_seconds_limit, 'case_seconds_remaining': round(remaining_time, 3)}
    for kind in ('chat', 'embedding'):
        for prefix, used, cap in ((kind+'_calls', ledger.case_counts[kind],
                                  ledger.case_limit(kind)),
                                 ('batch_'+kind+'_calls', ledger.counts[kind],
                                  getattr(limits, kind+'_calls'))):
            b.update({prefix+'_used': used, prefix+'_limit': cap,
                      prefix+'_remaining': max(0, cap-used)})
    return b


def diagnose(budget, history, text, *, program_error=None):
    """Observed conditions, not semantic grading or invented corpus availability."""
    if program_error:
        return 'PROGRAM_ERROR'
    if (budget['tool_calls_remaining'] == 0 or budget['chat_calls_remaining'] == 0
            or Decimal(budget['cost_usd_remaining']) == 0 or budget['case_seconds_remaining'] <= 0):
        return 'BUDGET_EXHAUSTED'
    if any(e['result']['status'] == 'error' for e in history):
        return 'TOOL_FAILURE'
    if history and all(e['result']['status'] == 'empty' for e in history):
        return 'RETRIEVAL_EMPTY_NOT_PROOF_OF_NONDISCLOSURE'
    if not history and re.search(r'预算.{0,10}(耗尽|为\s*0)|剩余工具.{0,12}[0零]|budget.{0,15}exhaust|no (remaining )?tool calls', text, re.IGNORECASE):
        return 'MODEL_BUDGET_MISREAD_SUSPECTED'
    return 'MODEL_FINISH_WITHOUT_EVIDENCE_REASON_NOT_INDEPENDENTLY_VERIFIED'


class BudgetModel(ContextModel):
    tool_used = 0

    def prepare(self, role, data):
        data = copy.deepcopy(data)
        if role != 'raw':
            data['budget'] = snapshot(self.ledger, self.tool_used)
            data['budget_contract'] = INSTRUCTION
        return super().prepare(role, data)

    async def dispatch(self, packet):
        # Packing may prepare multiple batches before any dispatch. Refresh each
        # non-tool packet against the current ledger, then recheck full capacity.
        # Research is prepared immediately and must retain native tool history.
        if packet.role not in ('raw', 'research'):
            packet = self.prepare(packet.role, packet.data)
        return await super().dispatch(packet)

    async def research_turn(self, context):
        # State counter incremented only by executor, not model or free-form text.
        self.tool_used = context['budget']['tool_calls']
        turn = await super().research_turn(context)
        if turn.action == 'finish' and not any(e['result'].get('data') for e in context['tool_events']):
            text = '\n'.join(c.get('text', '') for o in (self.adapter.last_output or {}).get('output', [])
                             for c in o.get('content', []) if c.get('type') == 'output_text')
            b = snapshot(self.ledger, self.tool_used)
            row = {'classification': diagnose(b, context['tool_events'], text),
                   'budget': b, 'tool_history': context['tool_events'],
                   'public_finish_text': text, 'automatic_corrective_calls': 0,
                   'semantic_abstention_validity': 'NOT_VERIFIED',
                   'corpus_unavailable': 'NOT_ESTABLISHED'}
            self.coverage['research_early_finish'] = row
            self.ledger.append('research_early_finish', case_id=self.ledger.case_id, **row)
        return turn


class BudgetWorkflow(ContextWorkflow):
    async def research(self, state):
        result = await super().research(state)
        self.adapter.tool_used = state.budget.tool_calls
        if not state.evidence and state.errors:
            self.adapter.coverage['research_executor_error'] = [e.code for e in state.errors]
        return result


class ExplicitBudgetExecution(TenExecution):
    def configuration(self):
        result = super().configuration()
        result.update(node_budget_contract=VERSION, research_early_finish='diagnose-preserve-no-auto-correction-v1')
        result['prompt_version'] += '-explicit-budget-v2'
        return result

    def model_wrapper(self, adapter):
        self.model = BudgetModel(adapter, self)
        return self.model

    def workflow(self, model, tools, settings, company_id, deadline, metadata):
        settings.max_tool_calls = 5  # Four searches plus the existing finish turn.
        model.repository = tools.repository
        return BudgetWorkflow(model, tools, settings, company_id, deadline, metadata)
